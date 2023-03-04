import copy
import logging
import math
import os
import sys
import time

import numpy as np

sys.path.append('/home/zju/wlj/SLBRIN')
from src.experiment.common_utils import load_data, Distribution, data_precision, data_region, load_query
from src.spatial_index.common_utils import biased_search_duplicate, binary_search_less_max, binary_search_duplicate, \
    Region, binary_search_less_max_duplicate
from src.spatial_index.geohash_utils import Geohash
from src.spatial_index.zm_index import Node
from src.spatial_index.zm_index_optimised import ZMIndexOptimised, NN
from src.ts_model import TimeSeriesModel

# 预设pagesize=4096, size(model)=2000, size(pointer)=4, size(x/y/geohash)=8
PAGE_SIZE = 4096
MODEL_SIZE = 2000
ITEM_SIZE = 8 * 3 + 4  # 28
MODELS_PER_PAGE = int(PAGE_SIZE / MODEL_SIZE)
ITEMS_PER_PAGE = int(PAGE_SIZE / ITEM_SIZE)


class TSUSLI(ZMIndexOptimised):
    def __init__(self, model_path=None):
        super().__init__(model_path)
        # for update
        self.start_time = 0
        self.time_id = 0
        self.time_interval = 0  # T
        self.lag = 0  # l
        self.predict_step = 0  # f
        self.cdf_width = 0  # c
        # for compute
        self.is_retrain = None
        self.is_save = None
        # insert_key time and io, merge_data time and io, retrain_model time, retrain_ts_model time
        self.statistic_list = [0, 0, 0, 0, 0, 0]

    def build_append(self, time_interval, start_time, end_time, is_retrain, is_save, lag, predict_step, cdf_width):
        """
        1. create delta_model with ts_model
        2. change delta_index from [] into [[]]
        """
        self.start_time = start_time
        self.time_id = math.ceil((end_time - start_time) / time_interval)
        self.time_interval = time_interval
        self.is_retrain = is_retrain
        self.is_save = is_save
        self.lag = lag
        self.predict_step = predict_step
        self.cdf_width = cdf_width
        retrain_ts_model_mse = [0, 0]
        # 1. create delta_model with ts_model
        for j in range(self.stages[-1]):
        # index_lens = [(j, len(self.rmi[-1][j].index)) for j in range(self.stages[-1])]
        # index_lens.sort(key=lambda x: x[-1])
        # max_len_index = index_lens[-1][0]
        # for j in [max_len_index]:
            node = self.rmi[-1][j]
            # create the old_cdfs and old_max_keys for delta_model
            min_key = node.model.input_min
            max_key = node.model.input_max
            key_interval = (max_key - min_key) / cdf_width
            key_list = [int(min_key + k * key_interval) for k in range(cdf_width)]
            old_cdfs = [[] for k in range(self.time_id)]
            for data in node.index:
                old_cdfs[(data[3] - self.start_time) // self.time_interval].append(data[2])
            old_max_keys = [max(len(cdf) - 1, 0) for cdf in old_cdfs]
            # for empty and head old_cdfs, remove them
            l = 0
            while l < self.time_id and len(old_cdfs[l]) == 0:
                l += 1
            old_cdfs = old_cdfs[l:]
            for k in range(len(old_cdfs)):
                cdf = old_cdfs[k]
                if cdf:  # for non-empty old_cdfs, create by data
                    old_cdfs[k] = self.build_cdf(cdf, key_list)
                else:  # for empty and non-head old_cdfs, copy from their previous
                    old_cdfs[k] = old_cdfs[k - 1]
            # plot_ts(cdfs)
            node.delta_model = TimeSeriesModel(key_list, self.model_path, self.time_id,
                                               old_cdfs, "var",
                                               old_max_keys, "es")
            mse_cdf, mse_max_key = node.delta_model.build(lag, predict_step, cdf_width)
            retrain_ts_model_mse[0] += mse_cdf
            retrain_ts_model_mse[1] += mse_max_key
            # 2. change delta_index from [] into [[]]
            node.delta_index = [[] for i in range(node.delta_model.max_keys[node.delta_model.time_id] + 1)]
        self.logging.info("Build ts model mse: %s" % retrain_ts_model_mse)

    def build_cdf(self, data, key_list):
        x_len = len(data)
        x_max_key = x_len - 1
        cdf = []
        p = 0
        for l in range(self.cdf_width):
            p = binary_search_less_max_duplicate(data, key_list[l], p, x_max_key)
            cdf.append(p / x_len)
        return cdf

    def insert_single(self, point):
        """
        different from zm_index
        1. find and insert ie into the target list of delta_index
        """
        gh = self.geohash.encode(point[0], point[1])
        point = (point[0], point[1], gh, point[2], point[3])
        node_key = self.get_leaf_node(gh)
        # 1. find and insert ie into the target list of delta_index
        tg_list = self.get_delta_index_list(gh, self.rmi[-1][node_key])
        tg_list_len = len(tg_list)
        tg_list.insert(binary_search_less_max(tg_list, 2, gh, 0, tg_list_len - 1) + 1, point)
        # IO1: search key
        self.io_cost += math.ceil(tg_list_len / ITEMS_PER_PAGE)

    def insert(self, points):
        """
        different from zm_index
        1. update once the time of new point cross the time interval
        """
        points = points.tolist()
        for point in points:
            cur_time = point[2]
            # update once the time of new point cross the time interval
            time_id = (cur_time - self.start_time) // self.time_interval
            if self.time_id < time_id:
                self.time_id = time_id
                self.update()
            start_time = time.time()
            io_cost = self.io_cost
            self.insert_single(point)
            self.statistic_list[0] += time.time() - start_time
            self.statistic_list[1] += self.io_cost - io_cost

    def update(self):
        """
        update the whole index
        1. merge delta index into index
        2. update model
        3. update delta model
        """
        leaf_nodes = self.rmi[-1]
        retrain_model_num = 0
        retrain_model_epoch = 0
        retrain_model_time = self.statistic_list[4]
        retrain_ts_model_num = 0
        retrain_ts_model_mse = [0, 0]
        retrain_ts_model_time = self.statistic_list[5]
        for j in range(0, self.stages[-1]):
            leaf_node = leaf_nodes[j]
            delta_index = []
            for tmp in leaf_node.delta_index:
                delta_index.extend(tmp)
            model = leaf_node.model
            if delta_index:
                # 1. merge delta index into index
                start_time = time.time()
                io_cost = self.io_cost
                if leaf_node.index:
                    leaf_node.index.extend(delta_index)
                    leaf_node.index.sort(key=lambda x: x[2])  # 优化：有序数组合并->sorted:2.5->1
                else:
                    leaf_node.index = delta_index
                self.statistic_list[2] += time.time() - start_time
                # IO1: merge data
                self.io_cost += math.ceil(len(leaf_node.index) / ITEMS_PER_PAGE)
                self.statistic_list[3] += self.io_cost - io_cost
                # 2. update model
                if self.is_retrain:
                    start_time = time.time()
                    inputs = [data[2] for data in leaf_node.index]
                    inputs.insert(0, model.input_min)
                    inputs.append(model.input_max)
                    inputs_num = len(inputs)
                    labels = list(range(0, inputs_num))
                    batch_size = 2 ** math.ceil(math.log(inputs_num / self.batch_num, 2))
                    if batch_size < 1:
                        batch_size = 1
                    model_key = "retrain_%s" % j
                    tmp_index = NN(self.model_path, model_key, inputs, labels, True, self.weight,
                                   self.cores, self.train_step, batch_size, self.learning_rate, False, None, None)
                    tmp_index.build_simple(None)  # retrain with initial model
                    # tmp_index.build_simple(model.matrices if model else None)  # retrain with old model
                    model.matrices = tmp_index.get_matrices()
                    model.output_max = inputs_num - 3
                    model.min_err = math.floor(tmp_index.min_err)
                    model.max_err = math.ceil(tmp_index.max_err)
                    end_time = time.time()
                    retrain_model_num += 1
                    retrain_model_epoch += tmp_index.get_epochs()
                    self.statistic_list[4] += end_time - start_time
                else:
                    time_model_path = os.path.join(self.model_path, "../zm_time_model", str(self.time_id))
                    index = ZMIndexOptimised(model_path=time_model_path)
                    index.load()
                    leaf_node.model = index.rmi[-1][j].model
                # 3. update delta model
                start_time = time.time()
                cur_cdf = self.build_cdf([data[2] for data in delta_index], leaf_node.delta_model.key_list)
                cur_max_key = len(delta_index) - 1
                num, mse_cdf, mse_max_key = leaf_node.delta_model.update(cur_cdf, cur_max_key, self.lag,
                                                                         self.predict_step, self.cdf_width)
                leaf_node.delta_index = [[] for i in range(leaf_node.delta_model.cur_max_key + 1)]
                end_time = time.time()
                retrain_ts_model_num += num
                retrain_ts_model_mse[0] += mse_cdf
                retrain_ts_model_mse[1] += mse_max_key
                self.statistic_list[5] += end_time - start_time
        if self.is_retrain:
            self.logging.info("Retrain model num: %s" % retrain_model_num)
            self.logging.info("Retrain model epoch: %s" % retrain_model_epoch)
            self.logging.info("Retrain model time: %s" % (self.statistic_list[4] - retrain_model_time))
        self.logging.info("Retrain ts model num: %s" % retrain_ts_model_num)
        self.logging.info("Retrain ts model mse: %s" % retrain_ts_model_mse)
        self.logging.info("Retrain ts model time: %s" % (self.statistic_list[5] - retrain_ts_model_time))
        if self.is_save:
            time_model_path = os.path.join(self.model_path, "../zm_time_model", str(self.time_id))
            if os.path.exists(time_model_path) is False:
                os.makedirs(time_model_path)
            index = copy.deepcopy(self)
            index.model_path = time_model_path
            index.save()

    def get_delta_index_list(self, key, leaf_node):
        """
        get the delta_index list which contains the key
        """
        pos = (key - leaf_node.model.input_min) / (
                leaf_node.model.input_max - leaf_node.model.input_min) * self.cdf_width
        pos_int = int(pos)
        left_p = leaf_node.delta_model.cur_cdf[pos_int]
        if pos >= self.cdf_width - 1:  # if point is at the top of cdf(1.0), insert into the tail of delta_index
            key = leaf_node.delta_model.cur_max_key
        else:
            right_p = leaf_node.delta_model.cur_cdf[pos_int + 1]
            key = int((left_p + (right_p - left_p) * (pos - pos_int)) * leaf_node.delta_model.cur_max_key)
        return leaf_node.delta_index[key]

    def point_query_single(self, point):
        """
        different from zm_index
        1. find the target list of delta_index which contains the target ie
        """
        gh = self.geohash.encode(point[0], point[1])
        leaf_node, _, pre, min_err, max_err = self.predict(gh)
        l_bound = max(pre - max_err, leaf_node.model.output_min)
        r_bound = min(pre - min_err, leaf_node.model.output_max)
        result = [leaf_node.index[key][4] for key in
                  biased_search_duplicate(leaf_node.index, 2, gh, pre, l_bound, r_bound)]
        self.io_cost += math.ceil((r_bound - l_bound) / ITEMS_PER_PAGE)
        # 1. find the target list of delta_index which contains the target ie
        if leaf_node.delta_index:
            tg_list = self.get_delta_index_list(gh, leaf_node)
            tg_list_len = len(tg_list)
            result.extend([tg_list[key][4] for key in binary_search_duplicate(tg_list, 2, gh, 0, tg_list_len - 1)])
            self.io_cost += math.ceil(tg_list_len / ITEMS_PER_PAGE)
        return result

    def save(self):
        """
        different from zm_index
        1. the delta_index isn't list but a list of list
        """
        meta = np.array((self.geohash.data_precision,
                         self.geohash.region.bottom, self.geohash.region.up,
                         self.geohash.region.left, self.geohash.region.right,
                         self.weight, self.train_step, self.batch_num, self.learning_rate),
                        dtype=[("0", 'i4'),
                               ("1", 'f8'), ("2", 'f8'), ("3", 'f8'), ("4", 'f8'),
                               ("5", 'f4'), ("6", 'i2'), ("7", 'i2'), ("8", 'f4')])
        np.save(os.path.join(self.model_path, 'meta.npy'), meta)
        meta_append = np.array((self.start_time, self.time_id, self.time_interval,
                                self.lag, self.predict_step, self.cdf_width),
                               dtype=[("0", 'i4'), ("1", 'i4'), ("2", 'i4'),
                                      ("13", 'i1'), ("14", 'i1'), ("15", 'i1')])
        np.save(os.path.join(self.model_path, 'meta_append.npy'), meta_append)
        np.save(os.path.join(self.model_path, 'stages.npy'), self.stages)
        np.save(os.path.join(self.model_path, 'cores.npy'), self.cores)
        models = []
        for stage in self.rmi:
            models.extend([node.model for node in stage])
        np.save(os.path.join(self.model_path, 'models.npy'), models)
        indexes = []
        index_lens = []
        delta_indexes = []
        delta_index_lens = []
        delta_models = []
        for node in self.rmi[-1]:
            indexes.extend(node.index)
            index_lens.append(len(node.index))
            delta_index = []
            for tmp in node.delta_index:
                delta_index.extend(tmp)
            delta_indexes.extend(delta_index)
            delta_index_lens.append(len(delta_index))
            delta_models.append(node.delta_model)
        np.save(os.path.join(self.model_path, 'indexes.npy'),
                np.array(indexes, dtype=[("0", 'f8'), ("1", 'f8'), ("2", 'i8'), ("3", 'i4'), ("4", 'i4')]))
        np.save(os.path.join(self.model_path, 'index_lens.npy'), index_lens)
        np.save(os.path.join(self.model_path, 'delta_indexes.npy'),
                np.array(delta_indexes, dtype=[("0", 'f8'), ("1", 'f8'), ("2", 'i8'), ("3", 'i4'), ("4", 'i4')]))
        np.save(os.path.join(self.model_path, 'delta_index_lens.npy'), delta_index_lens)
        np.save(os.path.join(self.model_path, 'delta_models.npy'), delta_models)

    def load(self):
        """
        different from zm_index
        1. the delta_index isn't [] but [[]]
        """
        meta = np.load(os.path.join(self.model_path, 'meta.npy'), allow_pickle=True).item()
        region = Region(meta[1], meta[2], meta[3], meta[4])
        self.geohash = Geohash.init_by_precision(data_precision=meta[0], region=region)
        self.stages = np.load(os.path.join(self.model_path, 'stages.npy'), allow_pickle=True).tolist()
        self.non_leaf_stage_len = len(self.stages) - 1
        self.cores = np.load(os.path.join(self.model_path, 'cores.npy'), allow_pickle=True).tolist()
        self.weight = meta[5]
        self.train_step = meta[6]
        self.batch_num = meta[7]
        self.learning_rate = meta[8]
        meta_append = np.load(os.path.join(self.model_path, 'meta_append.npy'), allow_pickle=True).item()
        self.start_time = meta_append[0]
        self.time_id = meta_append[1]
        self.time_interval = meta_append[2]
        self.lag = meta_append[3]
        self.predict_step = meta_append[4]
        self.cdf_width = meta_append[5]
        models = np.load(os.path.join(self.model_path, 'models.npy'), allow_pickle=True)
        indexes = np.load(os.path.join(self.model_path, 'indexes.npy'), allow_pickle=True).tolist()
        index_lens = np.load(os.path.join(self.model_path, 'index_lens.npy'), allow_pickle=True).tolist()
        delta_indexes = np.load(os.path.join(self.model_path, 'delta_indexes.npy'), allow_pickle=True).tolist()
        delta_index_lens = np.load(os.path.join(self.model_path, 'delta_index_lens.npy'), allow_pickle=True).tolist()
        delta_models = np.load(os.path.join(self.model_path, 'delta_models.npy'), allow_pickle=True)
        self.max_key = len(indexes)
        model_cur = 0
        self.rmi = []
        for i in range(len(self.stages)):
            if i < self.non_leaf_stage_len:
                self.rmi.append(
                    [Node(None, model, None, None) for model in models[model_cur:model_cur + self.stages[i]]])
                model_cur += self.stages[i]
            else:
                index_cur = 0
                delta_index_cur = 0
                leaf_nodes = []
                for j in range(self.stages[i]):
                    model = models[model_cur]
                    delta_index = delta_indexes[delta_index_cur:delta_index_cur + delta_index_lens[j]]
                    delta_model = delta_models[j]
                    delta_index_lists = [[] for i in range(delta_model.cur_max_key + 1)]
                    for tmp in delta_index:
                        pos = (tmp[2] - model.input_min) / (model.input_max - model.input_min) * self.cdf_width
                        pos_int = int(pos)
                        left_p = delta_model.cur_cdf[pos_int]
                        if pos >= self.cdf_width - 1:  # if point is at the top of cdf(1.0), insert into the tail of delta_index
                            key = delta_model.cur_max_key
                        else:
                            right_p = delta_model.cur_cdf[pos_int + 1]
                            key = int((left_p + (right_p - left_p) * (pos - pos_int)) * delta_model.cur_max_key)
                        delta_index_lists[key].append(tmp)
                    leaf_nodes.append(Node(indexes[index_cur:index_cur + index_lens[j]],
                                           models[model_cur],
                                           delta_index_lists,
                                           delta_model))
                    model_cur += 1
                    index_cur += index_lens[j]
                    delta_index_cur += delta_index_lens[j]
                self.rmi.append(leaf_nodes)

    def size(self):
        structure_size, ie_size = super(TSUSLI, self).size()
        structure_size += os.path.getsize(os.path.join(self.model_path, "meta_append.npy")) - 128
        ie_size += os.path.getsize(os.path.join(self.model_path, "delta_models.npy")) - 128
        return structure_size, ie_size


def main():
    load_index_from_json = True
    load_index_from_json2 = False
    os.chdir(os.path.dirname(os.path.realpath(__file__)))
    # model_path = "model/tsusli_nyct/"
    model_path = "model/tsusli_10w_nyct/"
    data_distribution = Distribution.NYCT_SORTED
    if os.path.exists(model_path) is False:
        os.makedirs(model_path)
    index = TSUSLI(model_path=model_path)
    index_name = index.name
    if load_index_from_json:
        super(TSUSLI, index).load()
    else:
        index.logging.info("*************start %s************" % index_name)
        start_time = time.time()
        build_data_list = load_data(data_distribution, 0)
        index.build(data_list=build_data_list,
                    is_sorted=True,
                    data_precision=data_precision[data_distribution],
                    region=data_region[data_distribution],
                    is_new=False,
                    is_simple=False,
                    weight=1,
                    stages=[1, 100],
                    cores=[[1, 32], [1, 32]],
                    train_steps=[5000, 5000],
                    batch_nums=[64, 64],
                    learning_rates=[0.001, 0.001],
                    use_thresholds=[False, False],
                    thresholds=[5, 20],
                    retrain_time_limits=[4, 2],
                    thread_pool_size=6)
        index.save()
        end_time = time.time()
        build_time = end_time - start_time
        index.logging.info("Build time: %s" % build_time)
    if load_index_from_json2:
        index.load()
    else:
        index.logging.info("*************start %s************" % index_name)
        start_time = time.time()
        index.build_append(time_interval=60 * 60,
                           start_time=1356998400,
                           end_time=1359676799,
                           is_retrain=False,
                           is_save=False,
                           lag=24,
                           predict_step=3,
                           cdf_width=100)
        index.save()
        end_time = time.time()
        build_time = end_time - start_time
        index.logging.info("Build time: %s" % build_time)
    structure_size, ie_size = index.size()
    logging.info("Structure size: %s" % structure_size)
    logging.info("Index entry size: %s" % ie_size)
    io_cost = 0
    logging.info("Model precision avg: %s" % index.model_err())
    point_query_list = load_query(data_distribution, 0).tolist()
    start_time = time.time()
    results = index.point_query(point_query_list)
    end_time = time.time()
    search_time = (end_time - start_time) / len(point_query_list)
    logging.info("Point query time: %s" % search_time)
    logging.info("Point query io cost: %s" % ((index.io_cost - io_cost) / len(point_query_list)))
    io_cost = index.io_cost
    np.savetxt(model_path + 'point_query_result.csv', np.array(results, dtype=object), delimiter=',', fmt='%s')
    update_data_list = load_data(Distribution.NYCT_10W, 1)
    start_time = time.time()
    index.insert(update_data_list)
    end_time = time.time()
    logging.info("Update time: %s" % (end_time - start_time))
    logging.info("Statis list: %s" % index.statistic_list)
    point_query_list = load_query(data_distribution, 0).tolist()
    start_time = time.time()
    results = index.point_query(point_query_list)
    end_time = time.time()
    search_time = (end_time - start_time) / len(point_query_list)
    logging.info("Point query time: %s" % search_time)
    logging.info("Point query io cost: %s" % ((index.io_cost - io_cost) / len(point_query_list)))
    io_cost = index.io_cost
    np.savetxt(model_path + 'point_query_result1.csv', np.array(results, dtype=object), delimiter=',', fmt='%s')


if __name__ == '__main__':
    main()
