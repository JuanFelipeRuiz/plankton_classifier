import os
import json
import logging
import pprint
import random
from collections import defaultdict

import torch
from PIL import Image
from torch.utils.data import Dataset
from torchvision import transforms
from torchvision.transforms.v2 import AugMix, Compose, Normalize, RandomHorizontalFlip, RandomRotation, Resize, ToDtype, ToImage

from ..helpers.helpers import define_priority_classes


class ImageFolderDataset(Dataset):
    """
    A Dataset subclass for managing and accessing image data stored in folders. This class supports optional
    image transformations, and Test Time Augmentation (TTA) for enhancing model evaluation during testing.

    Attributes:
        image_folder_path (str): Path to the folder containing image data.
        class_map_path (str): Path to the JSON file mapping class names to labels.
        priority_classes (str): Path to a JSON file specifying priority classes for targeted training or evaluation.
        train (bool): Specifies whether the dataset will be used for training. Determines the type of transformations applied.
        TTA (bool): Indicates if Test Time Augmentation should be applied during testing.
    """

    def __init__(self, image_folder_path: str, class_map_path: str, priority_classes: str, train: bool, TTA: bool = False):
        """
        Initializes the ImageFolderDataset with paths and modes.

        Args:
            image_folder_path (str): The folder path containing the images.
            class_map_path (str): The file path to the JSON file with class mappings.
            priority_classes (str): The file path to the JSON file that contains priority classes.
            train (bool): A flag to indicate if the dataset is used for training purposes.
            TTA (bool): A flag to enable Test Time Augmentation.
        """
        self.image_folder_path = image_folder_path
        self.TTA = TTA
        self.train = train
        self.class_map_path = class_map_path
        self.priority_classes = priority_classes

        # Load priority classes and adjust class map accordingly
        if self.priority_classes != []:

            logging.info(f"Priority classes not None. Loading priority classes from {self.priority_classes}")
            priority_postfix = "_priority"
            logging.info(f"Priority classes loaded: {self.priority_classes}")
            self.class_map_path = self.class_map_path.replace("class_map.json", f"class_map{priority_postfix}.json")
            logging.info(f"Class map path set to {self.class_map_path}")

        # Load class map from JSON or create it from the folder structure if not present
        if not os.path.exists(self.class_map_path):
            if not train:
                raise FileNotFoundError(f"Class map not found at {self.class_map_path}. Class map needs to be present for testing.")
            logging.info(f"Class map not found at {self.class_map_path}. Extracting class map from folder structure.")
            self._create_class_map(image_folder_path)
            logging.info(f"Class map saved to {self.class_map_path}")
        else:
            logging.info(f"Loading class map from {self.class_map_path}")
            with open(self.class_map_path, "r") as json_file:
                self.class_map = json.load(json_file)
            logging.info(f"Class map loaded.")

        # Transformation sequences for training and validation/testing
        self._define_transforms()
        # Load image information from the folder structure
        self.image_infos = self._load_image_infos()

    def _define_transforms(self):
        mean, std = [0.485, 0.456, 0.406], [0.229, 0.224, 0.225]  # ImageNet mean and std
        self.train_transforms = Compose([ToImage(), RandomHorizontalFlip(), Resize((224, 224)), ToDtype(torch.float32, scale=True), AugMix(), Normalize(mean, std)])
        self.val_transforms = Compose([ToImage(), Resize((224, 224)), ToDtype(torch.float32, scale=True), Normalize(mean, std)])
        if self.TTA:
            self.rotations = {
                "0": Compose([RandomRotation(0, 0)]),
                "90": Compose([RandomRotation((90, 90))]),
                "180": Compose([RandomRotation((180, 180))]),
                "270": Compose([RandomRotation((270, 270))]),
            }

    def __len__(self):
        """
        Returns the total number of images in the dataset.

        Returns:
            int: The total number of images.
        """
        return len(self.image_infos)

    def __getitem__(self, idx):
        """
        Retrieves an image and its corresponding label based on the provided index.

        Args:
            idx (int): The index of the image.

        Returns:
            tuple: A tuple containing the transformed image and its label.
        """
        image_info = self.image_infos[idx]
        image = Image.open(image_info).convert("RGB")
        # Apply TTA transformations if enabled
        if self.TTA:
            image = {rot: self.val_transforms(self.rotations[rot](image)) for rot in self.rotations}
        elif self.train:
            image = self.train_transforms(image)
        else:
            image = self.val_transforms(image)
        label = self.get_label_from_filename(image_info)
        return image, label

    def _load_image_infos(self):
        """
        Load image information from the folder structure.
        """
        image_infos = []
        for root, _, files in os.walk(self.image_folder_path):
            for file in files:
                if file.lower().endswith(("jpg", "jpeg", "png")):
                    image_infos.append(os.path.join(root, file))
        return image_infos

    def _create_class_map(self, folder_path):
        """
        Creates the class map from the folder structure and saves it to a JSON file.
        """
        logging.info("Creating class map from folder structure.")
        class_map = defaultdict(list)
        for root, dirs, files in os.walk(folder_path):
            for dir_name in dirs:
                class_map[dir_name] = []

        # Create a sorted list of class names and map them to indices
        sorted_class_names = sorted(class_map.keys())
        logging.info(f"Found {len(sorted_class_names)} classes.")
        self.class_map = {class_name: idx for idx, class_name in enumerate(sorted_class_names)}
        if self.priority_classes != []:

            logging.info(f'priority_classes not set to []. Defining priority class_map')
            for key in self.priority_classes:
                if key not in self.class_map.keys():
                    raise KeyError(f"Priority class {key} not found in class map. Keys of class map: {pprint.pformat(self.class_map.keys())}")
            self.class_map = define_priority_classes(self.priority_classes)

        logging.info(f"Class map created:\n{pprint.pformat(self.class_map)}")
        logging.info(f"Saving class map to {self.class_map_path}")
        os.makedirs(os.path.dirname(self.class_map_path), exist_ok=True)
        with open(self.class_map_path, "w") as json_file:
            json.dump(self.class_map, json_file, indent=4)

    def get_label_from_filename(self, filename):
        """
        Extracts the label index from a given filename.

        Args:
            filename (str): The filename from which to extract the label.

        Returns:
            int: The label index corresponding to the class.
        """
        label = filename.split(os.sep)[-2]
        label = self.class_map.get(label, 0)
        return label

    def shuffle(self):
        """
        Shuffles the list of image information to randomize data access, useful during training.
        """
        random.shuffle(self.image_infos)