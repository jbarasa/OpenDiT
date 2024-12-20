import logging
import math
import time
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from pprint import pformat
from typing import Iterator, List, Optional, Union

import torch
import torch.distributed as dist
from torch.utils.data import Dataset, DistributedSampler

from videosys.core.dcp.profiler import get_profiler

from .bucket import Bucket
from .datasets import DummyVariableVideoTextDataset, VariableVideoTextDataset

GB = 1024**3


# use pandarallel to accelerate bucket processing
# NOTE: pandarallel should only access local variables
def apply(data, method=None, frame_interval=None, seed=None, num_bucket=None):
    return method(
        data["num_frames"],
        data["height"],
        data["width"],
        frame_interval,
        seed + data["id"] * num_bucket,
    )


@dataclass
class BucketPlan:
    bucket_id: tuple
    batch_size: int
    sp_size: int
    exec_time: float


class StatefulDistributedSampler(DistributedSampler):
    def __init__(
        self,
        dataset: Dataset,
        num_replicas: Optional[int] = None,
        rank: Optional[int] = None,
        shuffle: bool = True,
        seed: int = 0,
        drop_last: bool = False,
    ) -> None:
        super().__init__(dataset, num_replicas, rank, shuffle, seed, drop_last)
        self.start_index: int = 0

    def __iter__(self) -> Iterator:
        iterator = super().__iter__()
        indices = list(iterator)
        indices = indices[self.start_index :]
        return iter(indices)

    def __len__(self) -> int:
        return self.num_samples - self.start_index

    def reset(self) -> None:
        self.start_index = 0

    def state_dict(self, step) -> dict:
        return {"start_index": step}

    def load_state_dict(self, state_dict: dict) -> None:
        self.__dict__.update(state_dict)


class VariableVideoBatchSampler(DistributedSampler):
    def __init__(
        self,
        dataset: Union[VariableVideoTextDataset, DummyVariableVideoTextDataset],
        bucket_config: dict,
        num_replicas: Optional[int] = None,
        rank: Optional[int] = None,
        shuffle: bool = True,
        seed: int = 0,
        drop_last: bool = False,
        verbose: bool = False,
        num_bucket_build_workers: int = 1,
        sp_balance_scope: str = "iter",
        auto_grad_accumulation: bool = False,
        max_grad_accumulation_steps: int = 5,
        parallel_mgr=None,
        calculate_imbalance: bool = False,
        min_grad_accumulation_steps: int = 2,
    ) -> None:
        super().__init__(
            dataset=dataset, num_replicas=num_replicas, rank=rank, shuffle=shuffle, seed=seed, drop_last=drop_last
        )
        self.dataset = dataset
        self.bucket = Bucket(bucket_config)
        self.verbose = verbose
        self.last_micro_batch_access_index = 0
        self.approximate_num_batch = None
        self.keep_last = not drop_last
        self._get_num_batch_cached_bucket_sample_dict = None
        self.num_bucket_build_workers = num_bucket_build_workers

        self.sp_balance_scope = sp_balance_scope
        self.auto_grad_accumulation = auto_grad_accumulation
        self.max_grad_accumulation_steps = max_grad_accumulation_steps
        self.min_grad_accumulation_steps = min_grad_accumulation_steps
        self.profiler = get_profiler()
        self.optimized_schedule = "local" if self.profiler.dynamic_sp else None
        self.generator = None
        if self.shuffle:
            self.generator = torch.Generator()
            self.generator.manual_seed(self.seed + self.epoch)
        self.cached_bucket_id_access_order = None
        self.effective_samples = 0
        self.parallel_mgr = parallel_mgr
        self.calculate_imbalance = calculate_imbalance
        self.imbalance_list = []
        self.est_total_execution_time = 0.0

    def __iter__(self) -> Iterator[List[int]]:
        if self._get_num_batch_cached_bucket_sample_dict is not None:
            bucket_sample_dict = self._get_num_batch_cached_bucket_sample_dict
            self._get_num_batch_cached_bucket_sample_dict = None
        else:
            bucket_sample_dict = self.group_by_bucket()
            if self.optimized_schedule is not None:
                self.get_num_batch_with_optimized_schedule(bucket_sample_dict)
            else:
                self.get_num_batch(bucket_sample_dict)

        if self.optimized_schedule is not None:
            yield from self._optimized_schedule_iter(bucket_sample_dict)
        else:
            yield from self._bucketized_iter(bucket_sample_dict)

    def change_timer_group(self, timers):
        cur_group = self.parallel_mgr.sp_group
        for t in timers:
            timers[t].group = cur_group

    def _build_bucketized_bucket_id_access_order(self, bucket_sample_dict):
        g = torch.Generator()
        g.manual_seed(self.seed + self.epoch)
        bucket_micro_batch_count = OrderedDict()
        self.effective_samples = 0

        # process the samples
        for bucket_id, data_list in bucket_sample_dict.items():
            ar_name, num_frame = bucket_id[:2]
            if not self.profiler.is_valid_bucket(ar_name, num_frame):
                if self.verbose:
                    logging.info(f"skip building batches for bucket {bucket_id} because it's invalid")
                continue
            # handle droplast
            bs_per_gpu = self.get_batch_size(bucket_id)
            org_num_samples = len(data_list)
            remainder = org_num_samples % bs_per_gpu

            if (not self.keep_last) and remainder > 0:
                # we just drop the remainder to make it divisible
                data_list = data_list[:-remainder]

            # handle shuffle
            if self.shuffle:
                data_indices = torch.randperm(len(data_list), generator=g).tolist()
                data_list = [data_list[i] for i in data_indices]
            bucket_sample_dict[bucket_id] = data_list

            # compute how many micro-batches each bucket has
            effect_size = len(data_list)
            if self.keep_last:
                num_micro_batches = (effect_size + bs_per_gpu - 1) // bs_per_gpu
                self.effective_samples += effect_size
            else:
                num_micro_batches = effect_size // bs_per_gpu
                self.effective_samples += bs_per_gpu * num_micro_batches
            bucket_micro_batch_count[bucket_id] = num_micro_batches

        # compute the bucket access order
        # each bucket may have more than one batch of data
        # thus bucket_id may appear more than 1 time
        bucket_id_access_order = []
        for bucket_id, num_micro_batch in bucket_micro_batch_count.items():
            bucket_id_access_order.extend([bucket_id] * num_micro_batch)

        # randomize the access order
        if self.shuffle:
            bucket_id_access_order_indices = torch.randperm(len(bucket_id_access_order), generator=g).tolist()
            bucket_id_access_order = [bucket_id_access_order[i] for i in bucket_id_access_order_indices]

        # make the number of bucket accesses divisible by dp size
        original_batches = len(bucket_id_access_order)
        remainder = original_batches % self.num_replicas
        bucket_num_batch_to_deduct = defaultdict(int)
        if remainder > 0:
            for i in range(original_batches - remainder, original_batches):
                bucket_num_batch_to_deduct[bucket_id_access_order[i]] += 1

            bucket_id_access_order = bucket_id_access_order[: original_batches - remainder]

            for bucket_id, num_batch_to_deduct in bucket_num_batch_to_deduct.items():
                total_samples = len(bucket_sample_dict[bucket_id])
                total_batches = bucket_micro_batch_count[bucket_id]

                left_batchs = total_batches - num_batch_to_deduct
                left_samples = left_batchs * self.get_batch_size(bucket_id)
                self.effective_samples -= total_samples - left_samples

        if self.verbose:
            for i in range(len(bucket_id_access_order)):
                logging.info(f"iter {i}, bucket_id: {bucket_id_access_order[i]}")
            logging.info(f"dropped: {pformat(bucket_num_batch_to_deduct, sort_dicts=False)}")
        return bucket_id_access_order

    def _bucketized_iter(self, bucket_sample_dict):
        bucket_last_consumed = OrderedDict()
        # acc_num_samples = torch.zeros(1, device=torch.cuda.current_device(), dtype=torch.float)
        if self.cached_bucket_id_access_order is not None:
            bucket_id_access_order = self.cached_bucket_id_access_order
            self.cached_bucket_id_access_order = None
        else:
            bucket_id_access_order = self._build_bucketized_bucket_id_access_order(bucket_sample_dict)

        # prepare each batch from its bucket
        # according to the predefined bucket access order
        num_iters = len(bucket_id_access_order) // self.num_replicas
        start_iter_idx = self.last_micro_batch_access_index // self.num_replicas

        # re-compute the micro-batch consumption
        # this is useful when resuming from a state dict with a different number of GPUs
        self.last_micro_batch_access_index = start_iter_idx * self.num_replicas
        for i in range(self.last_micro_batch_access_index):
            bucket_id = bucket_id_access_order[i]
            bucket_bs = self.get_batch_size(bucket_id)
            if bucket_id in bucket_last_consumed:
                bucket_last_consumed[bucket_id] += bucket_bs
            else:
                bucket_last_consumed[bucket_id] = bucket_bs

        self.est_total_execution_time = 0.0
        for i in range(start_iter_idx, num_iters):
            bucket_access_list = bucket_id_access_order[i * self.num_replicas : (i + 1) * self.num_replicas]
            self.last_micro_batch_access_index += self.num_replicas

            # compute the data samples consumed by each access
            bucket_access_boundaries = []
            for bucket_id in bucket_access_list:
                bucket_bs = self.get_batch_size(bucket_id)
                last_consumed_index = bucket_last_consumed.get(bucket_id, 0)
                bucket_access_boundaries.append([last_consumed_index, last_consumed_index + bucket_bs])

                # update consumption
                if bucket_id in bucket_last_consumed:
                    bucket_last_consumed[bucket_id] += bucket_bs
                else:
                    bucket_last_consumed[bucket_id] = bucket_bs

            if self.calculate_imbalance:
                total_time = []
                for bucket_id in bucket_access_list:
                    cur_time = self.profiler.get_execution_time(bucket_id[0], bucket_id[1])
                    total_time.append(cur_time)
                max_time = max(total_time)
                imbalance = sum([(max_time - t) for t in total_time]) / len(total_time)
                self.imbalance_list.append(imbalance)
                self.est_total_execution_time += max_time
                logging.info(
                    f"iter {i}, \nbucket_access_list: {bucket_access_list}\ntotal time: {total_time}"
                    f"\ncur imbalance: {imbalance/max_time*100:.4f} %, \nestimate total imbalance: {sum(self.imbalance_list) / len(self.imbalance_list) * num_iters:.4f}s"
                )

            # compute the range of data accessed by each GPU
            bucket_id = bucket_access_list[self.rank]
            boundary = bucket_access_boundaries[self.rank]
            cur_micro_batch = bucket_sample_dict[bucket_id][boundary[0] : boundary[1]]

            # encode t, h, w into the sample index
            real_t, real_h, real_w = self.bucket.get_thw(bucket_id)
            cur_micro_batch = [
                (idx, real_t, real_h, real_w, bucket_id[0], self.parallel_mgr.sp_size, 1) for idx in cur_micro_batch
            ]
            yield cur_micro_batch

        self.reset()

    def get_batch_size(self, bucket_id):
        bs_from_bucket_config = self.profiler.get_batch_size(bucket_id[0], bucket_id[1])
        return bs_from_bucket_config

    def __len__(self) -> int:
        bucket_sample_dict = self.group_by_bucket()
        self._get_num_batch_cached_bucket_sample_dict = bucket_sample_dict

        if self.optimized_schedule is not None:
            return self.get_num_batch_with_optimized_schedule(bucket_sample_dict)
        else:
            return self.get_num_batch(bucket_sample_dict) // self.num_replicas

    def group_by_bucket(self) -> dict:
        bucket_sample_dict = OrderedDict()

        from pandarallel import pandarallel

        pandarallel.initialize(nb_workers=self.num_bucket_build_workers, progress_bar=False, verbose=self.verbose)
        if self.verbose:
            logging.info(f"Building buckets...")
        bucket_ids = self.dataset.data.parallel_apply(
            apply,
            axis=1,
            method=self.bucket.get_bucket_id,
            frame_interval=self.dataset.frame_interval,
            seed=self.seed + self.epoch,
            num_bucket=self.bucket.num_bucket,
        )

        # group by bucket
        # each data sample is put into a bucket with a similar image/video size
        for i in range(len(self.dataset)):
            bucket_id = bucket_ids[i]
            if bucket_id is None:
                continue
            if bucket_id not in bucket_sample_dict:
                bucket_sample_dict[bucket_id] = []
            bucket_sample_dict[bucket_id].append(i)
        return bucket_sample_dict

    def _calculate_grad_accumulation_num(self, cur_first_batch_bucket_id_list):
        def score_func(new_time, median_time):
            if new_time > median_time:
                return (new_time - median_time) * 1.2
            else:
                return (median_time - new_time) * 1

        exec_time_list = [self.profiler.get_execution_time(*i[0][:2]) for i in cur_first_batch_bucket_id_list]
        max_time = max(exec_time_list) * self.max_grad_accumulation_steps
        min_diff = float("inf")
        num_gas = None
        for exec_time_outer in exec_time_list:
            max_mult_outer = int(max_time / exec_time_outer)
            for mult in range(1, max_mult_outer + 1):
                time_outer = exec_time_outer * mult
                if time_outer > max_time:
                    break
                gas_outer, diff_outer = [], 0
                for exec_time_inner in exec_time_list:
                    gas_inner, diff_inner = None, float("inf")
                    max_mult_inner = int(max_time / exec_time_inner)
                    for gas_val in range(1, max_mult_inner + 1):
                        time_inner = exec_time_inner * gas_val
                        if time_inner > max_time:
                            break
                        now_diff = score_func(time_inner, time_outer)
                        if now_diff < diff_inner:
                            diff_inner = now_diff
                            gas_inner = gas_val
                    diff_outer += diff_inner
                    gas_outer.append(gas_inner)
                if diff_outer < min_diff:
                    min_diff = diff_outer
                    num_gas = gas_outer

        # if max grad accumulation is less than min grad accumulation, repeat the grad accumulation
        if max(num_gas) < self.min_grad_accumulation_steps:
            grad_accumulation_steps = math.ceil(self.min_grad_accumulation_steps / max(num_gas))
            num_gas = [i * grad_accumulation_steps for i in num_gas]

        return num_gas

    def _build_local_bucket_id_access_order_acc(self, bucket_sample_dict):
        wsize = dist.get_world_size()
        bucket_id_access_order = []
        self.effective_samples = 0

        bucket_sp_map, sp_bucket_map = dict(), dict()
        for bucket_id, data_list in bucket_sample_dict.items():
            ar_name, num_frame = bucket_id[:2]
            if not self.profiler.is_valid_bucket(ar_name, num_frame):
                if self.verbose:
                    logging.info(f"skip building batches for bucket {bucket_id} because it's invalid")
                continue

            # collect bucket_sp_map, sp_bucket_map
            sp_size = self.profiler.get_sp_size(ar_name, num_frame)
            max_bs = self.profiler.get_batch_size(ar_name, num_frame)
            cur_len = len(data_list)
            remainder = cur_len % max_bs
            if (not self.keep_last) and remainder > 0:
                if self.drop_last:
                    data_list = data_list[:-remainder]
                else:
                    pad = max_bs - remainder
                    if pad > cur_len:
                        data_list = data_list * ((pad + cur_len - 1) // cur_len + 1)
                        data_list = data_list[: pad + cur_len]
                    else:
                        data_list += data_list[:pad]
            logging.info(f"bucket {bucket_id} original len: {cur_len} padded len: {len(data_list)} for bs {max_bs}")

            bucket_sp_map[bucket_id] = sp_size
            if sp_size not in sp_bucket_map:
                sp_bucket_map[sp_size] = []
            sp_bucket_map[sp_size].append(bucket_id)

            if self.generator is not None:
                data_indices = torch.randperm(len(data_list), generator=self.generator).tolist()
                data_list = [data_list[i] for i in data_indices]

            bucket_sample_dict[bucket_id] = data_list

        bucket_sample_dict_last_access = {k: 0 for k in bucket_sample_dict.keys()}
        sp_size_list = sorted(sp_bucket_map.keys())
        while sp_size_list:
            cur_first_batch_bucket_id_list = []
            remain_gpus = wsize
            has_one_more_batch = True
            while remain_gpus > 0:
                max_sp_idx = 0
                while max_sp_idx < len(sp_size_list) and remain_gpus >= sp_size_list[max_sp_idx]:
                    max_sp_idx += 1

                if max_sp_idx == 0:
                    # if false, cur_first_batch_bucket_id_list will be discarded
                    has_one_more_batch = False
                    break

                # select sp size
                if self.generator is not None:
                    cur_sp_size_list = sp_size_list[:max_sp_idx]
                    sp_size_sample_num = OrderedDict({k: len(sp_bucket_map[k]) for k in cur_sp_size_list})
                    total_samples = sum(sp_size_sample_num.values())
                    val = torch.rand(size=(1,), generator=self.generator).item()
                    idx = list(sp_size_sample_num.keys())[-1]
                    for k, v in sp_size_sample_num.items():
                        if val < v / total_samples:
                            idx = k
                            break
                    idx = sp_size_list.index(idx)
                else:
                    idx = max_sp_idx - 1
                sp = sp_size_list[idx]
                remain_gpus -= sp

                # select bucket id
                if self.generator is not None:
                    bucket_index = torch.randint(
                        low=0, high=len(sp_bucket_map[sp]), size=(1,), generator=self.generator
                    ).item()
                else:
                    bucket_index = 0
                bucket_id = sp_bucket_map[sp][bucket_index]
                ar_name, num_frame = bucket_id[:2]
                # max bs for first batch
                bs = self.profiler.get_batch_size(ar_name, num_frame)

                offset = bucket_sample_dict_last_access[bucket_id]
                num_samples = min(bs, len(bucket_sample_dict[bucket_id]) - offset)
                cur_first_batch_bucket_id_list.append((bucket_id, num_samples))

                offset += num_samples
                bucket_sample_dict_last_access[bucket_id] = offset
                if offset == len(bucket_sample_dict[bucket_id]):
                    sp_bucket_map[sp].pop(bucket_index)
                    if not sp_bucket_map[sp]:
                        sp_size_list.remove(sp)
                        sp_bucket_map.pop(sp)

                # to be more efficient, only pop when gpu is full
                if not self.keep_last and remain_gpus <= 0:
                    # get max grad accumulation
                    exec_time_list = [
                        self.profiler.get_execution_time(*i[0][:2]) for i in cur_first_batch_bucket_id_list
                    ]
                    num_gas = self._calculate_grad_accumulation_num(cur_first_batch_bucket_id_list)
                    max_time = max([exec_time * gas for exec_time, gas in zip(exec_time_list, num_gas)])
                    # remove bucket that is not enough for grad accumulation
                    index = 0
                    while index < len(cur_first_batch_bucket_id_list):
                        bucket_id, bs = cur_first_batch_bucket_id_list[index]
                        ar_name, num_frame = bucket_id[:2]
                        exec_time = self.profiler.get_execution_time(ar_name, num_frame)
                        max_bs = self.profiler.get_batch_size(ar_name, num_frame)
                        sp_size = self.profiler.get_sp_size(ar_name, num_frame)

                        required_gas = max_time // exec_time - 1
                        remain_batches = (
                            len(bucket_sample_dict[bucket_id]) - bucket_sample_dict_last_access[bucket_id]
                        ) // max_bs

                        # calculate repeat times for this bucket
                        bucket_id_list = [i[0] for i in cur_first_batch_bucket_id_list]
                        occur_times = bucket_id_list.count(bucket_id)
                        required_gas *= occur_times

                        if remain_batches < required_gas:
                            cur_first_batch_bucket_id_list.pop(index)
                            remain_gpus += sp_size
                            bucket_sample_dict_last_access[bucket_id] = len(bucket_sample_dict[bucket_id])
                            if sp_size in sp_bucket_map:
                                if bucket_id in sp_bucket_map[sp_size]:
                                    sp_bucket_map[sp_size].remove(bucket_id)
                                if not sp_bucket_map[sp_size]:
                                    sp_size_list.remove(sp_size)
                                    sp_bucket_map.pop(sp_size)
                        else:
                            index += 1

            if has_one_more_batch:
                # sort to make sure fitting
                cur_first_batch_bucket_id_list = sorted(
                    cur_first_batch_bucket_id_list, key=lambda x: bucket_sp_map[x[0]], reverse=True
                )

                if self.auto_grad_accumulation:
                    num_gas = self._calculate_grad_accumulation_num(cur_first_batch_bucket_id_list)
                else:
                    num_gas = [1 for _ in cur_first_batch_bucket_id_list]

                # decide accumulate batch_size
                # [[bucket_id, bs] * <num_acc for this seq>] * <num sp group for this iter>
                cur_batch_bucket_id_list = []
                batch_log = []
                # TODO: potential optimization to decide batch size and number of micro batches (for grad acc)
                for bidx, each in enumerate(cur_first_batch_bucket_id_list):
                    this_bucket_acc_list = [each]
                    bucket_id, max_bs = each
                    batch_log.append(
                        [
                            (bucket_id + (bucket_sp_map[bucket_id], max_bs)),
                        ]
                    )
                    # collect effective samples for the first batch of this iter
                    self.effective_samples += max_bs

                    # if has remaining samples for grad acc batch
                    offset = bucket_sample_dict_last_access[bucket_id]
                    total_len = len(bucket_sample_dict[bucket_id])
                    if offset < total_len:
                        ar_name, num_frame = bucket_id[:2]
                        sp = bucket_sp_map[bucket_id]

                        # here I use the max bs from profile
                        bs = max_bs

                        # minus one because of the first batch
                        num_acc = num_gas[bidx] - 1

                        while num_acc > 0 and offset < total_len:
                            num_samples = min(bs, total_len - offset)
                            this_bucket_acc_list.append((bucket_id, num_samples))

                            offset += num_samples
                            num_acc -= 1

                            # collect effective samples for grad acc batches of this iter
                            self.effective_samples += num_samples
                            batch_log[-1].append((bucket_id + (bucket_sp_map[bucket_id], num_samples)))

                        bucket_sample_dict_last_access[bucket_id] = offset
                        # remove exhausted buckets from local indices
                        if offset == total_len:
                            sp_bucket_map[sp].remove(bucket_id)
                            if not sp_bucket_map[sp]:
                                sp_size_list.remove(sp)
                                sp_bucket_map.pop(sp)

                    cur_batch_bucket_id_list.append(this_bucket_acc_list)
                logging.info(
                    f"iter {len(bucket_id_access_order)}, gas: {num_gas} actual: {[len(each) for each in cur_batch_bucket_id_list]}"
                    f", buckets: {batch_log}"
                )
                bucket_id_access_order.append(cur_batch_bucket_id_list)

        return bucket_id_access_order

    def _build_local_bucket_id_access_order_sp_balance(self, bucket_sample_dict):
        wsize = dist.get_world_size()
        bucket_id_access_order = []
        self.effective_samples = 0

        bucket_sample_counts = {}
        sp_bucket_map = dict()
        for bucket_id, data_list in bucket_sample_dict.items():
            ar_name, num_frame = bucket_id[:2]
            if not self.profiler.is_valid_bucket(ar_name, num_frame):
                if self.verbose:
                    logging.info(f"skip building batches for bucket {bucket_id} because it's invalid")
                continue

            # shuffle
            if self.generator is not None:
                data_indices = torch.randperm(len(data_list), generator=self.generator).tolist()
                data_list = [data_list[i] for i in data_indices]

            # record
            bucket_sample_dict[bucket_id] = data_list
            bucket_sample_counts[bucket_id] = len(data_list)
            sp_size = self.profiler.get_sp_size(ar_name, num_frame)
            if sp_size not in sp_bucket_map:
                sp_bucket_map[sp_size] = []
            sp_bucket_map[sp_size].append(bucket_id)

        sp_size_list = sorted(sp_bucket_map.keys())
        while sp_size_list:
            cur_batch_bucket_id_list = []
            remain_gpus = wsize
            has_one_more_batch = True
            while remain_gpus > 0:
                max_sp_idx = 0
                while max_sp_idx < len(sp_size_list) and remain_gpus >= sp_size_list[max_sp_idx]:
                    max_sp_idx += 1

                if max_sp_idx == 0:
                    # if false, cur_batch_bucket_id_list will be discarded
                    has_one_more_batch = False
                    break

                # select sp
                if self.generator is not None:
                    cur_sp_size_list = sp_size_list[:max_sp_idx]
                    probs = torch.tensor([len(sp_bucket_map[k]) for k in cur_sp_size_list], dtype=torch.float)
                    idx = torch.multinomial(probs, 1, generator=self.generator).item()
                else:
                    idx = max_sp_idx - 1
                sp = sp_size_list[idx]

                # select bucket
                if self.generator is not None:
                    bucket_index = torch.randint(0, len(sp_bucket_map[sp]), (1,), generator=self.generator).item()
                else:
                    bucket_index = 0
                bucket_id = sp_bucket_map[sp][bucket_index]
                ar_name, num_frame = bucket_id[:2]
                bs = self.profiler.get_batch_size(ar_name, num_frame)

                num_samples = min(bs, bucket_sample_counts[bucket_id])
                bucket_sample_counts[bucket_id] -= num_samples
                if bucket_sample_counts[bucket_id] == 0:
                    sp_bucket_map[sp].remove(bucket_id)
                    if not sp_bucket_map[sp]:
                        sp_size_list.remove(sp)
                        sp_bucket_map.pop(sp)

                exec_time = self.profiler.get_execution_time(ar_name, num_frame) / bs * num_samples
                cur_batch_bucket_id_list.append(
                    BucketPlan(
                        bucket_id=bucket_id,
                        batch_size=num_samples,
                        sp_size=sp,
                        exec_time=exec_time,
                    )
                )

                remain_gpus -= sp

                if not self.keep_last and len(cur_batch_bucket_id_list) > 1:
                    min_time_idx, min_time = -1, float("inf")
                    for i, each in enumerate(cur_batch_bucket_id_list):
                        max_bs = self.profiler.get_batch_size(*each.bucket_id[:2])
                        if each.exec_time < min_time and max_bs > each.batch_size:
                            min_time = each.exec_time
                            min_time_idx = i
                    if min_time_idx > -1:
                        # drop last batch for this bucket if it is the shortest time
                        pop_plan = cur_batch_bucket_id_list.pop(min_time_idx)
                        remain_gpus += pop_plan.sp_size

            if not has_one_more_batch:
                continue

            logging.info(
                f"iter {len(bucket_id_access_order)}\noriginal buckets: {[(each.bucket_id, each.batch_size, each.sp_size, each.exec_time) for each in cur_batch_bucket_id_list]}"
            )

            min_time, min_bucket = min(
                [(each.exec_time, each.bucket_id) for each in cur_batch_bucket_id_list], key=lambda x: x[0]
            )
            skip_bucket_idx = []
            if self.keep_last:
                no_last_batch, last_batch = [], []
                for i, bucket_plan in enumerate(cur_batch_bucket_id_list):
                    if bucket_sample_counts[bucket_plan.bucket_id] > 0:
                        no_last_batch.append(bucket_plan)
                    else:
                        last_batch.append(bucket_plan)

                if not no_last_batch:
                    assert len(last_batch) == len(cur_batch_bucket_id_list)
                    skip_bucket_idx = list(range(len(cur_batch_bucket_id_list)))
                    min_time = 0
                else:
                    min_time, min_bucket = min(
                        [(each.exec_time, each.bucket_id) for each in no_last_batch], key=lambda x: x[0]
                    )
                    skip_bucket_idx = []
                    if last_batch:
                        for i, bucket_plan in enumerate(cur_batch_bucket_id_list):
                            if bucket_plan.exec_time < min_time:
                                skip_bucket_idx.append(i)
            logs = []
            for i, bucket_plan in enumerate(cur_batch_bucket_id_list):
                if i in skip_bucket_idx or bucket_plan.bucket_id == min_bucket:
                    continue

                ar_name, num_frame = bucket_plan.bucket_id[:2]

                original_exec_time = bucket_plan.exec_time
                original_batch_size = bucket_plan.batch_size
                original_sp_size = bucket_plan.sp_size
                unit_time = original_exec_time / original_batch_size

                original_remain_samples = bucket_sample_counts[bucket_plan.bucket_id]

                best_diff = float("inf")
                best_exec_time, best_bs, best_sp = original_exec_time, original_batch_size, original_sp_size
                cur_sp_size = original_sp_size
                log_str = f"\n>>> bucket {bucket_plan.bucket_id}, bs: {original_batch_size}, sp: {original_sp_size}, time: {original_exec_time}"
                while cur_sp_size <= self.profiler.max_sp:
                    max_bs = self.profiler.detail_results[ar_name][num_frame][cur_sp_size]["bs"]
                    cur_unit_time = self.profiler.detail_results[ar_name][num_frame][cur_sp_size]["pred_time"] / max_bs

                    if max_bs - original_batch_size > original_remain_samples:
                        max_bs = original_batch_size + original_remain_samples

                    cur_bs = max(1, round(min_time / cur_unit_time))
                    if cur_bs > max_bs:
                        cur_bs = max_bs

                    cur_exec_time = cur_unit_time * cur_bs
                    cur_diff = abs(cur_exec_time - min_time)
                    log_str += (
                        f"\nCHECK sp: {cur_sp_size}, bs: {cur_bs}, time: {cur_exec_time}, diff: {cur_diff}/{best_diff}"
                    )
                    if cur_diff < best_diff:
                        best_diff = cur_diff
                        best_exec_time = cur_exec_time
                        best_bs = cur_bs
                        best_sp = cur_sp_size

                        if abs(cur_exec_time / min_time - 1) < 0.1:
                            break
                    cur_sp_size *= 2
                logs.append(log_str)
                assert (
                    best_bs > 0
                ), f"best_bs: {best_bs} - {original_batch_size}, best sp: {best_sp} - {original_sp_size}, best time: {best_exec_time} - {original_exec_time}, min time: {min_time}"

                # return left samples back to record
                if best_bs < cur_batch_bucket_id_list[i].batch_size:
                    bucket_id = cur_batch_bucket_id_list[i].bucket_id
                    org_sp = bucket_plan.sp_size
                    left_bs = cur_batch_bucket_id_list[i].batch_size - best_bs

                    bucket_sample_counts[bucket_id] += left_bs
                    if org_sp not in sp_bucket_map:
                        sp_bucket_map[org_sp] = []
                        sp_bucket_map[org_sp].append(bucket_id)
                        sp_size_list.append(org_sp)
                    else:
                        if sp_bucket_map[org_sp].count(bucket_id) == 0:
                            sp_bucket_map[org_sp].append(bucket_id)
                elif best_bs > cur_batch_bucket_id_list[i].batch_size:
                    bucket_id = cur_batch_bucket_id_list[i].bucket_id
                    org_sp = bucket_plan.sp_size

                    bucket_sample_counts[bucket_id] -= best_bs - cur_batch_bucket_id_list[i].batch_size
                    if bucket_sample_counts[bucket_id] == 0:
                        sp_bucket_map[org_sp].remove(bucket_id)
                        if not sp_bucket_map[org_sp]:
                            sp_size_list.remove(org_sp)
                            sp_bucket_map.pop(org_sp)

                cur_batch_bucket_id_list[i].batch_size = best_bs
                cur_batch_bucket_id_list[i].exec_time = best_exec_time
                cur_batch_bucket_id_list[i].sp_size = best_sp

            # pop and recover buckets out of limit
            cur_batch_bucket_id_list = sorted(cur_batch_bucket_id_list, key=lambda x: x.sp_size, reverse=True)
            total_gpus = sum([each.sp_size for each in cur_batch_bucket_id_list])
            poped = []
            while total_gpus > wsize:
                bucket_plan = cur_batch_bucket_id_list.pop()
                bucket_id = bucket_plan.bucket_id
                ar_name, num_frame = bucket_id[:2]
                org_sp = self.profiler.get_sp_size(ar_name, num_frame)
                sp = bucket_plan.sp_size
                bs = bucket_plan.batch_size

                bucket_sample_counts[bucket_id] += bs
                if org_sp not in sp_bucket_map:
                    sp_bucket_map[org_sp] = []
                    sp_bucket_map[org_sp].append(bucket_id)
                    sp_size_list.append(org_sp)
                elif sp_bucket_map[org_sp].count(bucket_id) == 0:
                    sp_bucket_map[org_sp].append(bucket_id)

                total_gpus -= sp
                poped.append(bucket_plan)
            assert total_gpus == wsize

            # rebalance bs only
            min_max_time, min_max_bucket = float("inf"), None
            for i, bucket_plan in enumerate(cur_batch_bucket_id_list):
                bucket_id = bucket_plan.bucket_id
                ar_name, num_frame = bucket_id[:2]
                cur_bs = bucket_plan.batch_size
                cur_sp = bucket_plan.sp_size
                cur_time = bucket_plan.exec_time
                unit_time = cur_time / cur_bs

                max_bs = self.profiler.detail_results[ar_name][num_frame][cur_sp]["bs"]
                if max_bs - cur_bs > bucket_sample_counts[bucket_id]:
                    max_bs = cur_bs + bucket_sample_counts[bucket_id]

                max_tmp_time = unit_time * max_bs
                if max_tmp_time < min_max_time:
                    min_max_time = max_tmp_time

            for i, bucket_plan in enumerate(cur_batch_bucket_id_list):
                bucket_id = bucket_plan.bucket_id
                # if bucket_id == min_max_bucket:
                #     continue

                ar_name, num_frame = bucket_id[:2]
                cur_sp = bucket_plan.sp_size
                max_bs = self.profiler.detail_results[ar_name][num_frame][cur_sp]["bs"]

                cur_exec_time = bucket_plan.exec_time
                cur_bs = bucket_plan.batch_size
                unit_time = cur_exec_time / cur_bs

                diff_time = min_max_time - cur_exec_time
                if diff_time <= 0:
                    continue

                increment_bs = int(diff_time // unit_time)
                if increment_bs + cur_bs > max_bs:
                    increment_bs = max_bs - cur_bs
                increment_bs = min(increment_bs, bucket_sample_counts[bucket_id])
                increment_time = unit_time * increment_bs

                if increment_bs > 0:
                    sp = self.profiler.get_sp_size(ar_name, num_frame)
                    bucket_sample_counts[bucket_id] -= increment_bs
                    if bucket_sample_counts[bucket_id] == 0:
                        sp_bucket_map[sp].remove(bucket_id)
                        if not sp_bucket_map[sp]:
                            sp_size_list.remove(sp)
                            sp_bucket_map.pop(sp)

                bucket_plan.batch_size += increment_bs
                bucket_plan.exec_time += increment_time
                assert (
                    bucket_plan.batch_size > 0
                ), f"increment_bs: {increment_bs}, cur_bs: {cur_bs}, max_bs: {max_bs}, increment_time: {increment_time}, cur_time: {cur_exec_time}, min_max_time: {min_max_time}"

            this_bucket_acc_list = []
            for bucket_plan in cur_batch_bucket_id_list:
                self.effective_samples += bucket_plan.batch_size
                this_bucket_acc_list.append(
                    [(bucket_plan.bucket_id, bucket_plan.batch_size, bucket_plan.sp_size, bucket_plan.exec_time)]
                )
            bucket_id_access_order.append(this_bucket_acc_list)
            logging.info(
                f"iter {len(bucket_id_access_order)}\nbuckets: {[(each.bucket_id, each.batch_size, each.sp_size, each.exec_time) for each in cur_batch_bucket_id_list]}"
                f"\npoped: {[(each.bucket_id, each.batch_size, each.sp_size, each.exec_time) for each in poped]}"
                f"\nmin time: {min_time:.2f}, max time: {min_max_time:.2f}"
                f"\n{logs}"
            )

        return bucket_id_access_order

    def _optimized_schedule_iter(self, bucket_sample_dict):
        rank, wsize = dist.get_rank(), dist.get_world_size()
        is_sp_balance_iter = (
            self.profiler.dynamic_sp
            and not self.profiler.dynamic_recompute
            and not self.auto_grad_accumulation
            and self.sp_balance_scope == "iter"
        )

        # bucket_id_access_order: [[(bucket_id, bs)] * <num acc of this bucket>] * <num sp groups of this iter>
        if self.cached_bucket_id_access_order is not None:
            bucket_id_access_order = self.cached_bucket_id_access_order
            self.cached_bucket_id_access_order = None
        elif is_sp_balance_iter:
            bucket_id_access_order = self._build_local_bucket_id_access_order_sp_balance(bucket_sample_dict)
        else:
            # support grad acc
            bucket_id_access_order = self._build_local_bucket_id_access_order_acc(bucket_sample_dict)

        num_iter = len(bucket_id_access_order)
        # skip resume code
        start_iter_idx = self.last_micro_batch_access_index
        self.est_total_execution_time = 0.0
        # generate execution plan
        bucket_last_consumed = OrderedDict()
        for i in range(start_iter_idx, num_iter):
            bucket_id_access_list = bucket_id_access_order[i]

            sp_size_map_list, bucket_id_map_list = [], []
            bucket_access_boundaries = []
            for bucket_list in bucket_id_access_list:
                boundary_gas_list = []
                for item in bucket_list:
                    bucket_id, bs = item[:2]

                    last_consumed_index = bucket_last_consumed.get(bucket_id, 0)
                    boundary_gas_list.append([last_consumed_index, last_consumed_index + bs])

                    if bucket_id in bucket_last_consumed:
                        bucket_last_consumed[bucket_id] += bs
                    else:
                        bucket_last_consumed[bucket_id] = bs
                    assert bucket_last_consumed[bucket_id] <= len(
                        bucket_sample_dict[bucket_id]
                    ), f"rank {rank} iter: {i}, bucket_id_access_list: {bucket_id_access_list}, bucket_last_consumed[{bucket_id}] = {bucket_last_consumed[bucket_id]} > {len(bucket_sample_dict[bucket_id])}"

                bucket_id = bucket_list[0][0]
                if is_sp_balance_iter:
                    sp_size = bucket_list[0][2]
                else:
                    sp_size = self.profiler.get_sp_size(bucket_id[0], bucket_id[1])

                sp_size_map_list.extend([sp_size] * sp_size)
                bucket_id_map_list.extend([bucket_list] * sp_size)
                bucket_access_boundaries.extend([boundary_gas_list] * sp_size)

            if self.calculate_imbalance:
                log_bucket_list, log_time_list = [], []
                for bucket_list in bucket_id_access_list:
                    bucket_id = bucket_list[0][0]

                    log_bucket_list.append(bucket_id)
                    if is_sp_balance_iter:
                        cur_time = bucket_list[0][3]
                    else:
                        cur_time = self.profiler.get_execution_time(bucket_id[0], bucket_id[1])
                    log_time_list.append(len(bucket_list) * cur_time)

                total_time = []
                for bucket_list in bucket_id_map_list:
                    gas = len(bucket_list)
                    bucket_id = bucket_list[0][0]
                    if is_sp_balance_iter:
                        cur_time = bucket_list[0][3]
                    else:
                        cur_time = self.profiler.get_execution_time(bucket_id[0], bucket_id[1])
                    cur_time = cur_time * gas
                    total_time.append(cur_time)
                max_time = max(total_time)
                imbalance = sum([(max_time - t) for t in total_time]) / len(total_time)
                self.imbalance_list.append(imbalance)
                self.est_total_execution_time += max_time
                logging.info(
                    f"iter {i}, \nbucket_id_map_list: {log_bucket_list}\ntotal time: {log_time_list}"
                    f"\ncur imbalance: {imbalance/max_time*100:.4f} %, \nestimate total imbalance: {sum(self.imbalance_list) / len(self.imbalance_list) * num_iter:.4f}s"
                )

            assert len(sp_size_map_list) == wsize
            sp_size = sp_size_map_list[rank]
            bucket_list = bucket_id_map_list[rank]
            boundaries = bucket_access_boundaries[rank]

            gas = len(bucket_list)
            cur_micro_batches = []
            for bucket, boundary in zip(bucket_list, boundaries):
                bucket_id, bs = bucket[:2]
                gas_micro_batches = bucket_sample_dict[bucket_id][boundary[0] : boundary[1]]
                assert (
                    len(gas_micro_batches) == bs
                ), f"iter {i}, rank {rank}, target bs: {bs}, actual bs: {len(gas_micro_batches)}"

                real_t, real_h, real_w = self.bucket.get_thw(bucket_id)
                cur_micro_batches.extend(
                    [(idx, real_t, real_h, real_w, bucket_id[0], sp_size, gas) for idx in gas_micro_batches]
                )

            assert (
                len(cur_micro_batches) > 0
            ), f"rank: {rank} iter: {i}, bucket_id_map_list: {bucket_id_map_list}, bucket_access_boundaries: {bucket_access_boundaries}"
            yield cur_micro_batches

        self.reset()

    def get_num_batch_with_optimized_schedule(self, bucket_sample_dict) -> int:
        start_ = time.time()
        if (
            self.profiler.dynamic_sp
            and not self.profiler.dynamic_recompute
            and not self.auto_grad_accumulation
            and self.sp_balance_scope == "iter"
        ):
            bucket_id_access_order = self._build_local_bucket_id_access_order_sp_balance(bucket_sample_dict)
            self.cached_bucket_id_access_order = bucket_id_access_order
        else:
            bucket_id_access_order = self._build_local_bucket_id_access_order_acc(bucket_sample_dict)
            self.cached_bucket_id_access_order = bucket_id_access_order
        self.approximate_num_batch = len(bucket_id_access_order)
        elapsed = time.time() - start_

        # collect statistics
        total_samples = 0
        bucket_stat_dict = dict()
        for k, v in bucket_sample_dict.items():
            ar_name, num_frame = k[:2]
            if not self.profiler.is_valid_bucket(ar_name, num_frame):
                continue
            size = len(v)
            max_bs = self.profiler.get_batch_size(ar_name, num_frame)
            if self.keep_last:
                effect_size = size + max_bs - 1
            else:
                effect_size = size
            num_batch = effect_size // max_bs
            if not self.keep_last:
                size = max_bs * num_batch

            total_samples += size

            bucket_stat_dict[k] = [size, num_batch]

        # log
        if dist.get_rank() == 0 and self.verbose:
            logging.info(f"Building index costs: {elapsed:.2f}s")
            logging.info(f"Bucket Info at epoch {self.epoch} with optimized schedule:")
            logging.info("Bucket [#sample, #batch]:\n%s", pformat(bucket_stat_dict, sort_dicts=False))
            logging.info(
                "#training batch: %s, #training sample: %s, #non empty bucket: %s",
                self.approximate_num_batch,
                total_samples,
                len(bucket_sample_dict),
            )

        return self.approximate_num_batch

    def get_num_batch(self, bucket_sample_dict) -> int:
        start_ = time.time()
        bucket_id_access_order = self._build_bucketized_bucket_id_access_order(bucket_sample_dict)
        self.cached_bucket_id_access_order = bucket_id_access_order
        self.approximate_num_batch = len(bucket_id_access_order)
        elapsed = time.time() - start_

        # collect statistics
        total_samples = 0
        total_batch = 0

        bucket_stat_dict = dict()
        for k, v in bucket_sample_dict.items():
            if not self.profiler.is_valid_bucket(k[0], k[1]):
                continue
            size = len(v)
            bs = self.get_batch_size(k)
            if self.keep_last:
                effect_size = size + bs - 1
            else:
                effect_size = size
            num_batch = effect_size // bs
            if not self.keep_last:
                size = bs * num_batch

            total_samples += size
            total_batch += num_batch

            bucket_stat_dict[k] = [size, num_batch]

        # log
        if dist.get_rank() == 0 and self.verbose:
            logging.info(f"Building index costs: {elapsed:.2f}s")
            logging.info(f"Bucket Info at epoch {self.epoch} with bucketized schedule:")
            logging.info("Bucket [#sample, #batch]:\n%s", pformat(bucket_stat_dict, sort_dicts=False))
            logging.info(
                "#training batch: %s, #training sample: %s, #non empty bucket: %s",
                total_batch,
                total_samples,
                len(bucket_sample_dict),
            )
        return self.approximate_num_batch

    def reset(self):
        if self.calculate_imbalance and len(self.imbalance_list) > 0:
            total_imbalance_time = sum(self.imbalance_list)
            logging.info(
                f"Total imbalance for this epoch: {total_imbalance_time:.2f}/{self.est_total_execution_time:.2f} ({total_imbalance_time/self.est_total_execution_time*100:.2f}%)"
            )
            self.imbalance_list = []
            self.est_total_execution_time = 0.0
        self.last_micro_batch_access_index = 0

    def state_dict(self, num_steps: int) -> dict:
        # the last_micro_batch_access_index in the __iter__ is often
        # not accurate during multi-workers and data prefetching
        # thus, we need the user to pass the actual steps which have been executed
        # to calculate the correct last_micro_batch_access_index
        return {"seed": self.seed, "epoch": self.epoch, "last_micro_batch_access_index": num_steps * self.num_replicas}

    def load_state_dict(self, state_dict: dict) -> None:
        self.__dict__.update(state_dict)
