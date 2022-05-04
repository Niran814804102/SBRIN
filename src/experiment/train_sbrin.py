import logging
import os
import sys
import time

import numpy as np

sys.path.append('/home/zju/wlj/st-learned-index')
from src.spatial_index.common_utils import Region
from src.spatial_index.sbrin import SBRIN


if __name__ == '__main__':
    os.chdir(os.path.dirname(os.path.realpath(__file__)))
    parent_path = "model/sbrin/tn"
    if not os.path.exists(parent_path):
        os.makedirs(parent_path)
    logging.basicConfig(filename=os.path.join(parent_path, "log.file"),
                        level=logging.INFO,
                        format="%(message)s")
    # 1. 读取数据
    # data_path = '../../data/index/trip_data_1_filter_10w_sorted.npy'
    data_path = '../../data/index/trip_data_1_filter_sorted.npy'
    # 2. 设置实验参数
    tn_list = [160000, 80000, 40000, 20000, 10000, 5000]
    # 3. 开始实验
    # 3.1 快速构建精度低的
    for tn in tn_list:
        model_path = "model/sbrin/tn/%s/" % tn
        if not os.path.exists(model_path):
            os.makedirs(model_path)
        index = SBRIN(model_path=model_path)
        index_name = index.name
        logging.info("*************start %s************" % model_path)
        start_time = time.time()
        data_list = np.load(data_path, allow_pickle=True)
        index.build(data_list=data_list,
                    threshold_number=tn,
                    data_precision=6,
                    region=Region(40, 42, -75, -73),
                    threshold_err=0,
                    threshold_summary=1000,
                    threshold_merge=5,
                    use_threshold=False,
                    threshold=0,
                    core=[1, 128, 1],
                    train_step=5000,
                    batch_num=64,
                    learning_rate=0.1,
                    retrain_time_limit=0,
                    thread_pool_size=12,
                    save_nn=True,
                    weight=1)
        index.save()
        end_time = time.time()
        build_time = end_time - start_time
        logging.info("Build time: %s" % build_time)
        logging.info("Index size: %s" % index.size())
        model_num = index.meta.last_hr + 1
        logging.info("Model num: %s" % model_num)
        model_precisions = [(hr.model.max_err - hr.model.min_err)
                            for hr in index.history_ranges]
        model_precisions_avg = sum(model_precisions) / model_num
        logging.info("Model precision avg: %s" % model_precisions_avg)
        path = '../../data/query/point_query.npy'
        point_query_list = np.load(path, allow_pickle=True).tolist()
        start_time = time.time()
        index.test_point_query(point_query_list)
        end_time = time.time()
        search_time = (end_time - start_time) / len(point_query_list)
        logging.info("Point query time: %s" % search_time)
        path = '../../data/query/range_query.npy'
        range_query_list = np.load(path, allow_pickle=True).tolist()
        for i in range(len(range_query_list) // 1000):
            tmp_range_query_list = range_query_list[i * 1000:(i + 1) * 1000]
            range_ratio = tmp_range_query_list[0][4]
            start_time = time.time()
            index.test_range_query(tmp_range_query_list)
            end_time = time.time()
            search_time = (end_time - start_time) / 1000
            logging.info("Range query ratio:  %s" % range_ratio)
            logging.info("Range query time:  %s" % search_time)
        path = '../../data/query/knn_query.npy'
        knn_query_list = np.load(path, allow_pickle=True).tolist()
        for i in range(len(knn_query_list) // 1000):
            tmp_knn_query_list = knn_query_list[i * 1000:(i + 1) * 1000]
            n = tmp_knn_query_list[0][2]
            start_time = time.time()
            index.test_knn_query(tmp_knn_query_list)
            end_time = time.time()
            search_time = (end_time - start_time) / 1000
            logging.info("KNN query n:  %s" % n)
            logging.info("KNN query time:  %s" % search_time)