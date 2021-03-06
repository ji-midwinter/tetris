﻿from __future__ import print_function
import tensorflow as tf
import numpy as np
from collections import deque
from time import sleep
import time
import datetime
import random
import copy
from game import Tetris
from play import TetrisUI
import d_model_1 as using_model
import mcts

# [OK]现在计算targetQ太慢，考虑：1、合并批次，同同一个批次计算。2、每次生成记录时直接计算，然后在target session更新时重新计算一遍。
# [OK]reward 函数需要按照论文重新写
# [OK]优先清扫还没有实现，如果实现这个，也需要在memory中预先保存Q和targetQ——实现了基本的数据准备，包括权重计算额更新，目前缺少sample函数。这个功能主要是提升效率
# [OK]关于奖励函数的转换，从启发式规则到分数驱动——这个功能会提高后期效果，还需要考虑一下怎么做，目前的思路是，从gold中加载模型，然后用新的奖励函数去训练
# [OK]现在准备了5、6两个模型，到时候可以分别测试一下——现在看差不多，但是从速度上，还是选择5
# [OK]现在对于层次低的时候效果不错，但是摞高了就不行了，考虑针对有一定高度的时候进行强化训练。比如让游戏每次初始化都有一个随机的高度？
# ————加了高层的训练数据后，结果被玩坏了，看来明天得从gold重新开始训练
# [OK]可以考虑一个重置学习步骤的操作，这样对调整学习率有好处——不过感觉还是直接调整学习率好一点，毕竟训练次数是一个记录
# [OK]关于探索步骤的选择，可以让随机探索的步骤，在模型计算的前几个步骤中产生，这样的训练效果明显好一些
# 现在的模型会出现空洞，是否可以提高空洞的惩罚
# ——只提高了空洞的惩罚，但是并不好用，还是会出现空洞，我理解，是因为消去空洞会获得的奖励与制造空洞所产生的惩罚一样多，所以ai会制造空洞，然后再去消除，获得更高的分数。
# ——所以，我打算尝试将空洞的奖励做成不对称的，制造的惩罚要大于消去的奖励
# ——经测试，这个方法貌似效果也不好

# [OK]需要针对-A的明星写一个模型评测的算法了

'''
目前的最佳模型 model_5_best_51：
一model_5_gold 为基础
评价函数：score = -0.510066 * aggregate_height + 0.760666 * complete_lines - 0.35663 * holes - 0.184483 * bumpiness
训练次数：2000，学习率 0.0001
最高份51分

2017-12-26
新提交的版本，学习率0.0001 golden基础上30000次：max: 146, min: 31, avg: 72
'''

model = None
sess = None
saver = None
is_new_model = False
is_master = False
fix_learningrate = 0
save_path = ""

def init_model(train = False, forceinit = False, init_with_gold = False, learning_rate = 0):
	global model
	global sess
	global saver
	global is_new_model
	global save_path
	
	# 初始化模型和路径
	model = using_model.create_model_5()
	save_path = "model_5/"
	gold_save_path = "model_5_gold/"
	
	with model.as_default():
		global_step = tf.Variable(0, name="step")

	if train:
		create_train_op(model, learning_rate=learning_rate)
	
	# sess_config=tf.ConfigProto(device_count={"CPU":1}, inter_op_parallelism_threads=0, intra_op_parallelism_threads=0)
	# sess = tf.Session(graph = model, config=sess_config) #这里使用普通的Session，是为了使用多个sess做准备
	sess = tf.Session(graph = model) #这里使用普通的Session，是为了使用多个sess做准备

	with model.as_default():
		saver = tf.train.Saver(max_to_keep = 1)

		cp = tf.train.latest_checkpoint(save_path)
		if cp == None or forceinit:
			if init_with_gold:
				print("init model with gold val")
				restore_model(sess, gold_save_path)
			else:
				print("init model with default val")
				tf.global_variables_initializer().run(session=sess)
				is_new_model = True
			save_model()
		else:
			print("init model with saved val")
			saver.restore(sess, cp)
			is_new_model = False

def save_model():
	global sess
	global saver
	global save_path
	saver.save(sess, save_path + 'save.ckpt')

def restore_model(dst_sess, src_path = None):
	global saver
	global save_path
	if src_path == None:
		src_path = save_path
	cp = tf.train.latest_checkpoint(src_path)
	if cp != None:
		saver.restore(dst_sess, cp)
	else:
		print("restore model fail.")

__cur_step = -1
__cur_action = 0
def run_game(tetris):
	global model
	global sess
	global __cur_step
	global __cur_action
	if tetris.step() != __cur_step:
		status = train_make_status(tetris)
		__cur_step = tetris.step()
		_, __cur_action = train_getMaxQ(status, model, sess)
		print("step %d, score: %d, action: %d" % (__cur_step, tetris.score(), __cur_action))

	x, r = train_getxr_by_action(__cur_action)
	if tetris.move_step_by_ai(x, r):
		tetris.fast_finish()


def create_train_op(model, learning_rate):
	global fix_learningrate

	with model.as_default():
		#train input
		_action = tf.placeholder(tf.float32, [None, 40], name="action")
		_targetQ = tf.placeholder(tf.float32, [None], name="targetQ") # reward + gamma * max(Q_sa)

		#train
		Q = model.get_tensor_by_name("output:0")		
		cost = tf.reduce_mean(tf.square(Q - _targetQ), name="cost")
		
		global_step = model.get_tensor_by_name("step:0")
		init_lr = 1e-4
		decay_lr = tf.train.exponential_decay(init_lr, global_step, decay_steps=1500, decay_rate=0.98, staircase=True, name="lr")
		if learning_rate == 0:
			# 0.98 ^ 100 = 0.13，所以X00表示每XW次训练，学习率降低1个数量级
			optimizer = tf.train.AdamOptimizer(decay_lr).minimize(cost, name="train_op", global_step=global_step)
			fix_learningrate = 0
			print("decay learning rate with init value: %f" % init_lr)
		else:
			optimizer = tf.train.AdamOptimizer(learning_rate).minimize(cost, name="train_op", global_step=global_step)
			fix_learningrate = learning_rate
			print("fix learning rate: %f" % learning_rate)

	return model

# MASTER_TRAIN_COUNT = 50000
def train(tetris
	, memory_size = 1000
	, batch_size = 50
	, train_steps = 10000
	, gamma = 0.6
	, init_epsilon = 1
	, min_epsilon = 0.01
	, as_master = False
	, printPerStep = 100
	, upgateTargetAndSavePerStep = 1000
	, ui = None):
	global model
	global sess
	global is_new_model
	global fix_learningrate
	global is_master
	D = deque()

	# Debug
	# batch_size = 1
	# printPerStep = 1
	# Finish Debug

	target_sess = tf.Session(graph = model)
	restore_model(target_sess)
	global_step = sess.run(model.get_tensor_by_name("step:0"))
	is_master = as_master

	if not is_new_model:
		init_epsilon = float(init_epsilon) / 2

	epsilon = init_epsilon
	step = 0
	status_0 = train_make_status(tetris)
	print("train start at: " + time.strftime('%Y-%m-%d %H:%M:%S',time.localtime(time.time())) \
			 + ", global step: " + str(global_step) + ", Master: " + str(is_master))
	while True:
		#run game
		action_0 = 0
		if is_master: # 我修改的算法，在高级模式下，让动作采集来自现有的策略中的前几个
			Action_s = train_getActionArrayByQ(status_0, model, sess)
			if random.random() < epsilon:
				selectable_size = int(len(Action_s) / 4)
				action_0 = Action_s[random.randrange(0, selectable_size)]
			else:
				action_0 = Action_s[0]

			# selectable_size = len(Action_s) / 4
			# t = float(step) / float(train_steps)
			# k = train_lerp(0, 0.9, t)
			# p_s = [(selectable_size - i) * k for i in range(selectable_size)]
			# p_s_softmax = train_softmax(p_s)
			# # print(p_s)
			# # print(p_s_softmax)
			# action_0 = np.random.choice(Action_s[0:selectable_size], p = train_softmax(p_s_softmax))
			# # t = float(step) / float(train_steps)
			# # rand_action_size = int(train_lerp(5, 1, t))
			# # action_0 = Action_s[random.randrange(rand_action_size)]
		else: # 经典deepQ算法
			if random.random() < epsilon:
				action_0 = random.randrange(len(train_getValidAction(status_0)))
			else:
				_, action_0 = train_getMaxQ(status_0, model, sess)
		epsilon = init_epsilon + (min_epsilon - init_epsilon) * step / train_steps

		gameover = train_run_game(tetris, action_0, ui)  #use the action to run, then get reward
		if gameover:
			print("train game over, score: %d, step: %d" % (tetris.score(), tetris.step()))
			tetris.reset()
		status_1 = train_make_status(tetris)
		reward_1, reward_info = train_cal_reward(tetris, gameover)

		Q_0 = train_getQ([status_0], [action_0], model, sess)[0]
		targetMaxQ_1, _ = train_getMaxQ(status_1, model, target_sess)
		priproity = abs(Q_0 - reward_1 - targetMaxQ_1) #loss is priproity
		
		#log to memory
		D.append([status_0, action_0, Q_0, reward_1, status_1, targetMaxQ_1, gameover, priproity])
		if len(D) > memory_size:
			D.popleft()

		if ui != None:
			ui.log("reward: %f, info: %s" % (reward_1, reward_info))

		#review memory
		if len(D) > batch_size:
			# batch = random.sample(D, batch_size)
			batch = train_sample(D, batch_size)
			status_0_batch = [d[0] for d in batch]
			action_0_batch = [d[1] for d in batch]
			reward_1_batch = [d[3] for d in batch]
			status_1_batch = [d[4] for d in batch]
			targetMaxQ_1_batch = [d[5] for d in batch]
			gameover_1_batch = [d[6] for d in batch]

			t_caltarget_begin = datetime.datetime.now()
			targetQ_batch = []
			for i in range(len(batch)):
				if gameover_1_batch[i]:
					targetQ_batch.append(reward_1_batch[i])
				else:
					targetQ_batch.append(reward_1_batch[i] + gamma * targetMaxQ_1_batch[i])
			t_caltarget_use = datetime.datetime.now() - t_caltarget_begin

			from_s = []
			to_s = []
			next_s = []
			for i in range(len(status_0_batch)):
				_from, _to, _next = train_simlutate_status_for_model_input(status_0_batch[i], action_0_batch[i])
				from_s.append(_from)
				to_s.append(_to)
				next_s.append(_next)

			t_trainnet_begin = datetime.datetime.now()
			_, _output, _cost, global_step, _lr = sess.run((model.get_operation_by_name("train_op")
				, model.get_tensor_by_name("output:0")
				, model.get_tensor_by_name("cost:0")
				, model.get_tensor_by_name("step:0")
				, model.get_tensor_by_name("lr:0")
				)
				, feed_dict={"from:0":from_s, "to:0":to_s, "next:0":next_s, "targetQ:0":targetQ_batch, "kp:0":0.75})
			t_trainnet_use = datetime.datetime.now() - t_trainnet_begin

			for i in range(len(batch)):
				batch[i][2] = _output[i]	# 更新记忆中的Q值，下次采样会用到
				train_update_sample_rate(batch[i])

			if step % printPerStep == 0:
				match_cnt = 0
				for i in range(batch_size):
					if targetQ_batch[i] != 0 and float(abs(_output[i] - targetQ_batch[i])) / float(abs(targetQ_batch[i])) < 0.1:
						match_cnt += 1
				match_rate = float(match_cnt) / float(batch_size)
				using_lr = fix_learningrate if fix_learningrate > 0 else _lr
				info = "train step %d(g: %d), epsilon: %f, lr: %f, action[0]: %d, reward[0]: %f, targetQ[0]: %f, Q[0]: %f, matchs: %f, cost: %f (time: %d/%d)" \
						% (step, global_step, epsilon, using_lr, action_0_batch[0], reward_1_batch[0], targetQ_batch[0], _output[0], match_rate, _cost \
						, t_caltarget_use.microseconds, t_trainnet_use.microseconds)
				if ui == None:
					print(info)
					if printPerStep == 1:	#为了调试，这样能看清楚日志
						sleep(1)
				else:
					ui.log(info)

			if step % upgateTargetAndSavePerStep == 0:
				print("update target session...")
				restore_model(target_sess)
				x = 0
				for memory in D:
					memory[5], _ = train_getMaxQ(memory[4], model, target_sess)
					train_update_sample_rate(memory)
					x += 1
					# if x % 100 == 0:
					# 	#print("update target maxQ: %d/%d" % (x, len(D)))
					# 	print("...", end='')

				print("save model...")
				save_model()
		#loop
		status_0 = status_1
		step += 1
		if step > train_steps:
			break

	print("train finish at: " + time.strftime('%Y-%m-%d %H:%M:%S',time.localtime(time.time())))

def train_softmax(x):
	return np.exp(x) / np.sum(np.exp(x), axis = 0)

def train_lerp(x0, x1, t):
	return x0 + (x1 - x0) * t

def train_update_sample_rate(m):
	m[7] = abs(m[2] - m[3] - m[5])

def train_sample(D, size):
	# https://docs.scipy.org/doc/numpy-1.13.0/reference/generated/numpy.random.choice.html
	priproity = [d[7] for d in D]
	s = float(sum(priproity))
	normal_priproity = [float(p) / s for p in priproity]

	idxs = np.random.choice(len(D), size, False, normal_priproity)
	batch = []
	for i in idxs:
		batch.append(D[i])

	# bi = "bi: "
	# for m in batch:
	# 	bi += str(m[7]) + ", "
	# si = "si: "
	# for m in D:
	# 	si += str(m[7]) + ", "
	# print(bi)
	# print(si)
	return batch

def train_make_status(tetris):	# 0, tiles; 1, current
	w = tetris.width()
	h = tetris.height()
	image = [[ 0 for x in range(w) ] for y in range(h)]
	
	tiles = tetris.tiles()
	for y in range(0, h):
		for x in range(0, w):
			if tiles[y][x] > 0:
				image[y][x] = 1
	
	_current = tetris.current_index()
	_next = tetris.next_index()
	_score = tetris.score()
	_step = tetris.step()
	status = {"tiles":image, "current":_current, "next":_next, "score":_score, "step":_step}
	return status

def train_getQ(status_s, action_s, use_model, use_sess):
	from_s = []
	to_s = []
	next_s = []
	for i in range(len(status_s)):
		_from, _to, _next = train_simlutate_status_for_model_input(status_s[i], action_s[i])
		from_s.append(_from)
		to_s.append(_to)
		next_s.append(_next)

	Q = use_sess.run(use_model.get_tensor_by_name("output:0"), feed_dict={"from:0":from_s, "to:0":to_s, "next:0":next_s, "kp:0":1.0})
	return Q

def train_getMaxQ(status, use_model, use_sess):
	Q_s = train_getQ_Array(status, use_model, use_sess)
	return max(Q_s), np.argmax(Q_s)

def train_getQ_Array(status, use_model, use_sess):
	status_s = []
	action_s = []
	for i in train_getValidAction(status):
		status_s.append(status)
		action_s.append(i)
	Q_s = train_getQ(status_s, action_s, use_model, use_sess)
	return Q_s

def train_getActionArrayByQ(status, use_model, use_sess):
	Q_s = train_getQ_Array(status, use_model, use_sess)
	Action_s = []
	for i in range(len(Q_s)):
		a = np.argmax(Q_s)
		Q_s[a] = -10000
		Action_s.append(a)
	return Action_s

def train_getMaxQ_batch(status_batch, use_model, use_sess):
	status_s = []
	action_s = []
	for status in status_batch:
		for i in train_getValidAction(status):
			status_s.append(status)
			action_s.append(i)
	Q_s = train_getQ(status_s, action_s, use_model, use_sess)

	maxQ_batch = []
	maxAction_batch = []
	p = 0
	for status in status_batch:
		actLen = len(train_getValidAction(status))
		smallQ_s = Q_s[p:p+actLen]
		p += actLen
		maxQ_batch.append(max(smallQ_s))
		maxAction_batch.append(np.argmax(smallQ_s))

	return maxQ_batch, maxAction_batch

def train_getValidAction(status):
	#根据不同的形状，这里可以优化，不是所有形状都需要探索0~40
	current = status["current"]
	if current == 1: #方块，不用旋转
		return range(10)
	elif current == 0 or current == 3 or current == 4: #长条和两个S
		return range(20)
	else:
		return range(40)

_simulator = Tetris()
def train_simlutate_status_for_model_input(status, action):
	global _simulator
	# def apply_status_by_ai(self, nodes, _current, _next, _score, _step)
	_simulator.apply_status_by_ai(nodes = status["tiles"], _current = status["current"], _next = status["next"], _score = status["score"], _step = status["step"])
	image_from = train_capture_model_input_image(_simulator)
	next_index = _simulator.next_index()
	train_run_game(_simulator, action, None)
	image_to = train_capture_model_input_image(_simulator)
	return image_from, image_to, next_index

def train_capture_model_input_image(tetris):
	w = tetris.width()
	h = tetris.height()
	image = [[ 0 for x in range(w)] for y in range(h)]
	
	tiles = tetris.tiles()
	for y in range(0, h):
		for x in range(0, w):
			if tiles[y][x] > 0:
				image[y][x] = 1

	current = tetris.current()
	for t in current:
		image[t[1]][t[0]] = 1
	return image

def train_getxr_by_action(action):
	r = int(action / 10)
	x = int(action % 10)
	return x, r

def train_run_game(tetris, action, ui):
	x, r = train_getxr_by_action(action)

	while True:
		move_finish = tetris.move_step_by_ai(x, r)

		if ui != None:
			if ui.refresh_and_check_quit():
				raise Exception("user quit")

		if move_finish:
			tetris.fast_finish()
			break

	return tetris.gameover()

# s_last_score = 0
s_last_aggregate_height = 0
s_last_holes = 0
s_last_bumpiness = 0

def train_reset_reward_status():
	# global s_last_score
	global s_last_aggregate_height
	global s_last_holes
	global s_last_bumpiness
	# s_last_score = 0
	s_last_aggregate_height = 0
	s_last_holes = 0
	s_last_bumpiness = 0

def train_heuristic_score(aggregate_height, complete_lines, holes, bumpiness): # 启发性评分规则
	# global is_master
	# if is_master: #对于训练次数达到一定程度的模型，增加这个奖励
	# 	score = -0.510066 * aggregate_height + 0.760666 * complete_lines * complete_lines - 0.35663 * holes - 0.184483 * bumpiness
	# else:
	# 	score = -0.510066 * aggregate_height + 0.760666 * complete_lines - 0.35663 * holes - 0.184483 * bumpiness

	score = -0.510066 * aggregate_height + 0.760666 * complete_lines - 0.35663 * holes - 0.184483 * bumpiness
	return score

def train_cal_reward(tetris, gameover = False):
	# https://codemyroad.wordpress.com/2013/04/14/tetris-ai-the-near-perfect-player/
	# 这个函数还可以调整，按照论文，还可以修改为从启发式规则到游戏得分的过度
	# global s_last_score
	global s_last_aggregate_height
	global s_last_holes
	global s_last_bumpiness
	global is_master

	if gameover:
		train_reset_reward_status()
		return -100, "game over"

	complete_lines = tetris.last_erase_row()
	# ——经过测试，下面的方法并不好，导致训练效果差很多，感觉上，缺少了指导，单纯得分为导向的评价，确实很难训练。在最后增加分数的奖励，训练多次之后，确实可以达到追求多层的效果
	# if global_step > MASTER_TRAIN_COUNT: #对于训练次数达到一定程度的模型，仅仅使用消去作为奖励，这样是为了消去的更多
	# 	return complete_lines * complete_lines, "complete_lines: %d" % complete_lines

	column_height = [0] * 10
	holes = 0

	tiles = tetris.tiles()
	for y in range(len(tiles)):
		height = 20-y
		row = tiles[y]
		for x in range(len(row)):
			t = row[x]
			if t > 0:
				column_height[x] = max(column_height[x], height)
			elif height < column_height[x]:
				holes += 1

	aggregate_height = sum(column_height)
	aggregate_height_just_clear = complete_lines * 10 #刚刚消去的行数要记入当前操作评价的分数，但是不作为下一次分数的比较
	bumpiness = sum([abs(column_height[i] - column_height[i+1]) for i in range(9)])

	reward = train_heuristic_score(aggregate_height + aggregate_height_just_clear - s_last_aggregate_height
		, complete_lines
		#, (holes - s_last_holes) if (holes > s_last_holes) else 0
		, holes - s_last_holes
		, bumpiness - s_last_bumpiness)

	# reward = score - s_last_score
	info = "aggregate_height: %d, complete_lines: %d, holes: %d, bumpiness: %d" % (aggregate_height, complete_lines, holes, bumpiness)

	# # 为下一次操作记录评分时，消除行数为0，否则会影响下一次的计算
	# s_last_score = train_heuristic_score(aggregate_height, 0, holes, bumpiness)
	s_last_aggregate_height = aggregate_height
	s_last_holes = holes
	s_last_bumpiness = bumpiness

	if is_master:
		reward += complete_lines * complete_lines

	return reward, info

if __name__ == '__main__':
	init_model()
	save_model()
