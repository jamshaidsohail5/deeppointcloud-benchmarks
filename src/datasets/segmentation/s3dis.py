import os
import os.path as osp
from itertools import repeat, product
import numpy as np
import pandas as pd
import torch
import h5py
import torch
import glob
from torch_geometric.data import InMemoryDataset, Data, download_url, extract_zip, Dataset
from torch_geometric.data.dataset import files_exist
from torch_geometric.data import DataLoader
from torch_geometric.datasets import S3DIS as S3DIS1x1
import torch_geometric.transforms as T
import logging
from sklearn.neighbors import NearestNeighbors
from tqdm import tqdm as tq
import csv
import pandas as pd
from src.metrics.segmentation_tracker import SegmentationTracker
from src.metrics.s3dis_tracker import S3DISTracker
from src.datasets.samplers import BalancedRandomSampler
import src.core.data_transform as cT
from src.datasets.base_dataset import BaseDataset
import pickle

log = logging.getLogger(__name__)

S3DIS_NUM_CLASSES = 13

INV_OBJECT_LABEL = {
    0: "ceiling",
    1: "floor",
    2: "wall",
    3: "beam",
    4: "column",
    5: "window",
    6: "door",
    7: "chair",
    8: "table",
    9: "bookcase",
    10: "sofa",
    11: "board",
    12: "clutter",
}

OBJECT_LABEL = {name: i for i, name in INV_OBJECT_LABEL.items()}

################################### UTILS #######################################


def object_name_to_label(object_class):
    """convert from object name in S3DIS to an int"""
    object_label = OBJECT_LABEL.get(object_class, OBJECT_LABEL["clutter"])
    return object_label


def read_s3dis_format(train_file, room_name, label_out=True, verbose=False, debug=False):
    """extract data from a room folder"""
    raw_path = osp.join(train_file, "{}.txt".format(room_name))
    if debug:
        reader = pd.read_csv(raw_path, delimiter="\n")
        RECOMMENDED = 6
        for idx, row in enumerate(reader.values):
            row = row[0].split(" ")
            if len(row) != RECOMMENDED:
                log.info("1: {} row {}: {}".format(raw_path, idx, row))

            try:
                for r in row:
                    r = float(r)
            except:
                log.info("2: {} row {}: {}".format(raw_path, idx, row))

        return True
    else:
        room_ver = pd.read_csv(raw_path, sep=" ", header=None).values
        xyz = np.ascontiguousarray(room_ver[:, 0:3], dtype="float32")
        try:
            rgb = np.ascontiguousarray(room_ver[:, 3:6], dtype="uint8")
        except ValueError:
            rgb = np.zeros((room_ver.shape[0], 3), dtype="uint8")
            log.warning("WARN - corrupted rgb data for file %s" % raw_path)
        if not label_out:
            return xyz, rgb
        n_ver = len(room_ver)
        del room_ver
        nn = NearestNeighbors(1, algorithm="kd_tree").fit(xyz)
        room_labels = np.zeros((n_ver,), dtype="int64")
        room_object_indices = np.zeros((n_ver,), dtype="int64")
        objects = glob.glob(osp.join(train_file, "Annotations/*.txt"))
        i_object = 1
        for single_object in objects:
            object_name = os.path.splitext(os.path.basename(single_object))[0]
            if verbose:
                log.debug("adding object " + str(i_object) + " : " + object_name)
            object_class = object_name.split("_")[0]
            object_label = object_name_to_label(object_class)
            obj_ver = pd.read_csv(single_object, sep=" ", header=None).values
            _, obj_ind = nn.kneighbors(obj_ver[:, 0:3])
            room_labels[obj_ind] = object_label
            room_object_indices[obj_ind] = i_object
            i_object = i_object + 1

        return (
            torch.from_numpy(xyz),
            torch.from_numpy(rgb),
            torch.from_numpy(room_labels),
            torch.from_numpy(room_object_indices),
        )


def add_weights(dataset, train, class_weight_method):
    L = len(INV_OBJECT_LABEL.keys())
    if train:
        weights = torch.ones(L)
        if class_weight_method is not None:

            idx_classes, counts = torch.unique(dataset.data.y, return_counts=True)

            dataset.idx_classes = torch.arange(L).long()
            weights[idx_classes] = counts.float()
            weights = weights.float()
            weights = weights.mean() / weights
            if class_weight_method == "sqrt":
                weights = torch.sqrt(weights)
            elif str(class_weight_method).startswith("log"):
                w = float(class_weight_method.replace("log", ""))
                weights = 1 / torch.log(1.1 + weights / weights.sum())

            weights /= torch.sum(weights)
        log.info(
            "CLASS WEIGHT : {}".format(
                {name: np.round(weights[index].item(), 4) for index, name in INV_OBJECT_LABEL.items()}
            )
        )
        setattr(dataset, "weight_classes", weights)
    else:
        setattr(dataset, "weight_classes", torch.ones((len(INV_OBJECT_LABEL.keys()))))

    return dataset


################################### DATASETS ###################################

################################### 1m cylinder s3dis ###################################


class S3DIS1x1Dataset(BaseDataset):
    def __init__(self, dataset_opt):
        super().__init__(dataset_opt)

        pre_transform = self.pre_transform

        transform = T.Compose(
            [T.FixedPoints(dataset_opt.num_points), T.RandomTranslate(0.01), T.RandomRotate(180, axis=2),]
        )

        train_dataset = S3DIS1x1(
            self._data_path,
            test_area=self.dataset_opt.fold,
            train=True,
            pre_transform=self.pre_transform,
            transform=self.train_transform,
        )
        self.test_dataset = S3DIS1x1(
            self._data_path,
            test_area=self.dataset_opt.fold,
            train=False,
            pre_transform=pre_transform,
            transform=self.test_transform,
        )

        self.train_dataset = add_weights(train_dataset, True, dataset_opt.class_weight_method)

    @staticmethod
    def get_tracker(model, dataset, wandb_log: bool, tensorboard_log: bool):
        """Factory method for the tracker

        Arguments:
            dataset {[type]}
            wandb_log - Log using weight and biases
        Returns:
            [BaseTracker] -- tracker
        """
        return SegmentationTracker(dataset, wandb_log=wandb_log, use_tensorboard=tensorboard_log)


################################### Used for fused s3dis radius sphere ###################################


class S3DISOriginalFused(InMemoryDataset):

    url = "https://docs.google.com/forms/d/e/1FAIpQLScDimvNMCGhy_rmBA2gHfDu3naktRm6A8BPwAWWDv-Uhm6Shw/viewform?c=0&w=1"
    zip_name = "Stanford3dDataset_v1.2_Version.zip"
    folders = ["Area_{}".format(i) for i in range(1, 7)]
    num_classes = S3DIS_NUM_CLASSES

    def __init__(
        self,
        root,
        test_area=6,
        train=True,
        transform=None,
        pre_transform=None,
        pre_collate_transform=None,
        pre_filter=None,
        keep_instance=False,
        verbose=False,
        debug=False,
    ):
        assert test_area >= 1 and test_area <= 6
        self.transform = transform
        self.pre_collate_transform = pre_collate_transform
        self.test_area = test_area
        self.keep_instance = keep_instance
        self.verbose = verbose
        self.debug = debug
        super(S3DISOriginalFused, self).__init__(root, transform, pre_transform, pre_filter)
        path = self.processed_paths[0] if train else self.processed_paths[1]
        self.data, self.slices = torch.load(path)

        if not train:
            self.raw_test_data = torch.load(self.raw_areas_paths[test_area - 1])

    @property
    def center_labels(self):
        if hasattr(self.data, "center_label"):
            return self.data.center_label
        else:
            return None

    @property
    def raw_file_names(self):
        return self.folders

    @property
    def pre_processed_path(self):
        test_area = self.test_area
        pre_processed_file_names = "pre_{}.pt".format(test_area)
        return os.path.join(self.processed_dir, pre_processed_file_names)

    @property
    def raw_areas_paths(self):
        return [os.path.join(self.processed_dir, "raw_area_%i.pt" % i) for i in range(6)]

    @property
    def processed_file_names(self):
        test_area = self.test_area
        return (
            ["{}_{}.pt".format(s, test_area) for s in ["train", "test"]]
            + self.raw_areas_paths
            + [self.pre_processed_path]
        )

    @property
    def raw_test_data(self):
        return self._raw_test_data

    @raw_test_data.setter
    def raw_test_data(self, value):
        self._raw_test_data = value

    def download(self):
        raw_folders = os.listdir(self.raw_dir)
        if len(raw_folders) == 0:
            raise RuntimeError(
                "Dataset not found. Please download {} from {} and move it to {} with {}".format(
                    self.zip_name, self.url, self.raw_dir, self.folders
                )
            )
        else:
            intersection = len(set(self.folders).intersection(set(raw_folders)))
            if intersection == 0:
                log.info("The data seems properly downloaded")
            else:
                raise RuntimeError(
                    "Dataset not found. Please download {} from {} and move it to {} with {}".format(
                        self.zip_name, self.url, self.raw_dir, self.folders
                    )
                )

    def process(self):

        if not os.path.exists(self.pre_processed_path):

            train_areas = [f for f in self.folders if str(self.test_area) not in f]
            test_areas = [f for f in self.folders if str(self.test_area) in f]

            train_files = [
                (f, room_name, osp.join(self.raw_dir, f, room_name))
                for f in train_areas
                for room_name in os.listdir(osp.join(self.raw_dir, f))
                if os.path.isdir(osp.join(self.raw_dir, f, room_name))
            ]

            test_files = [
                (f, room_name, osp.join(self.raw_dir, f, room_name))
                for f in test_areas
                for room_name in os.listdir(osp.join(self.raw_dir, f))
                if os.path.isdir(osp.join(self.raw_dir, f, room_name))
            ]

            train_files = train_files[1:10]
            test_files = test_files[1:10]

            # Gather data per area
            data_list = [[] for _ in range(6)]
            for (area, room_name, file_path) in tq(train_files + test_files):

                area_num = int(area[-1]) - 1
                if self.debug:
                    read_s3dis_format(file_path, room_name, label_out=True, verbose=self.verbose, debug=self.debug)
                    continue
                else:
                    xyz, rgb, room_labels, room_object_indices = read_s3dis_format(
                        file_path, room_name, label_out=True, verbose=self.verbose, debug=self.debug
                    )

                    rgb_norm = rgb.float() / 255.0
                    data = Data(pos=xyz, y=room_labels, rgb=rgb_norm)

                    if self.keep_instance:
                        data.room_object_indices = room_object_indices

                    if self.pre_filter is not None and not self.pre_filter(data):
                        continue

                    data_list[area_num].append(data)

            raw_areas = cT.PointCloudFusion()(data_list)
            for i, area in enumerate(raw_areas):
                torch.save(area, self.raw_areas_paths[i])

            for area_datas in data_list:
                # Apply pre_transform
                if self.pre_transform is not None:
                    for data in area_datas:
                        data = self.pre_transform(data)

            torch.save(data_list, self.pre_processed_path)
        else:
            data_list = torch.load(self.pre_processed_path)

        if self.debug:
            return

        train_data_list = [data_list[i] for i in range(6) if (i != self.test_area - 1 and len(data_list[i]))]
        test_data_list = data_list[self.test_area - 1]

        if self.pre_collate_transform:
            log.info("pre_collate_transform ...")
            train_data_list = self.pre_collate_transform(train_data_list)
            test_data_list = self.pre_collate_transform(test_data_list)

        torch.save(self.collate(train_data_list), self.processed_paths[0])
        torch.save(self.collate(test_data_list), self.processed_paths[1])


class S3DISFusedDataset(BaseDataset):
    def __init__(self, dataset_opt):
        super().__init__(dataset_opt)

        self.train_dataset = S3DISOriginalFused(
            self._data_path,
            test_area=self.dataset_opt.fold,
            train=True,
            pre_collate_transform=self.pre_collate_transform,
            transform=self.train_transform,
        )
        self.test_dataset = S3DISOriginalFused(
            self._data_path,
            test_area=self.dataset_opt.fold,
            train=False,
            pre_collate_transform=self.pre_collate_transform,
            transform=self.test_transform,
        )

        if dataset_opt.class_weight_method:
            self.train_dataset = add_weights(self.train_dataset, True, dataset_opt.class_weight_method)

        if dataset_opt.sampler:
            self.train_sampler = BalancedRandomSampler(self.train_dataset.center_labels)
        else:
            self.train_sampler = None

    @property
    def test_data(self):
        return self.test_dataset[0].raw_test_data

    @staticmethod
    def get_tracker(model, dataset, wandb_log: bool, tensorboard_log: bool):
        """Factory method for the tracker

        Arguments:
            task {str} -- task description
            dataset {[type]}
            wandb_log - Log using weight and biases
        Returns:
            [BaseTracker] -- tracker
        """
        return S3DISTracker(dataset, wandb_log=wandb_log, use_tensorboard=tensorboard_log)
