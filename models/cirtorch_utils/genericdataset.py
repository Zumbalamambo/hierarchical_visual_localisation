import os
import pdb
import random
import numpy as np

import torch
import torch.utils.data as data
from torch_geometric.data import Data as GeoData


from torch_geometric.data import Batch as GeoBatch


from models.cirtorch_utils.datahelpers import default_loader, imresize

class PCDataLoader(data.DataLoader):
    
    def __collate__(self, data_list, follow_batch=[]):
        img_pos = torch.stack([d[0] for d in data_list])
        pt_pos = GeoBatch.from_data_list([GeoData(pos=d[1]) for d in data_list], follow_batch)
        if data_list[0][2] is not None:
            img_negs = torch.stack([d[2] for d in data_list])
            pt_negs = GeoBatch.from_data_list([GeoData(pos=d[3]) for d in data_list], follow_batch)
        else:
            img_negs = None
            pt_negs = None
        return [img_pos, pt_pos, img_negs, pt_negs]
    
    def __init__(self,
                 dataset,
                 batch_size=1,
                 shuffle=False,
                 follow_batch=[],
                 **kwargs):
        super(PCDataLoader, self).__init__(
            dataset,
            batch_size,
            shuffle,
            collate_fn=lambda data_list: self.__collate__(data_list, follow_batch),
            **kwargs)

class PointCloudSplit(data.Dataset):
    """
    Every split=15 item is chosen as validation sample.
    If val then validation samples are returned
    Else training samples are returned
    """
    def __init__(self, point_list, val=False, split=15):
        self.point_list = point_list
        self.idcs = []
        for i in range(len(point_list)):
            if i % split == 0:
                if val:
                    self.idcs.append(i)
            else:
                if not val:
                    self.idcs.append(i)
        
    def __getitem__(self, index, val=False):
        idx = self.idcs[index]
        return self.point_list[idx]
    
    def __len__(self):
        return len(self.idcs)
        
        
        
    

class PointCloudImagesFromList(data.Dataset):
    """
    Based on ImagesFromList
    """
    def __init__(self, root, images, points3d, imsize=None, transform=None, loader=default_loader, triplet=False, 
                min_num_points=100, overfit=-1, max_std_std=0.1, within_std = 1.0, demean=True, deterministic=False):
        self.image_ids = []
        self.images_fn = []
        self.overfit = overfit
        self.max_std_std = max_std_std
        self.within_std = within_std
        self.demean = demean
        self.deterministic = deterministic
        for i, img in enumerate(images.keys()):
            if overfit > 0 and i > overfit:
                break
            valid = images[img].point3D_ids > 0
            if self.max_std_std < float('inf'):                
                pt_ids = images[img].point3D_ids[valid]
                pts = np.stack([points3d[i].xyz for i in pt_ids])
                mean, std = np.mean(pts, axis=0), np.std(pts, axis=0)
                if self.within_std > 0:
                    valid_pts = np.all(np.abs(pts - mean) < self.within_std*std, axis=1)
                    pts = pts[valid_pts]
                std = np.std(pts, axis=0)
                std_std = np.std(std) / np.mean(std) ##normalize std
                if std_std > self.max_std_std:
                    continue
            num_valid = valid.sum()
            if num_valid > min_num_points:
                self.image_ids.append(img)
                self.images_fn.append(os.path.join(root,images[img].name))
        #self.image_ids = {i : k for i, k in enumerate(images.keys())}
        #self.images_fn = [os.path.join(root,images[k].name) for i, k in self.image_ids.items()]
        if len(self.images_fn) == 0:
            raise(RuntimeError("Dataset contains 0 images!"))
        self.root = root
        self.images = images
        self.points3d = points3d
        self.imsize = imsize
        self.transform = transform
        self.loader = loader
        self.triplet = triplet
        random.seed(0)
        
    def __getitem__(self, index):
        """
        Args:
            index (int): Index

        Returns:
            image (PIL): Loaded image
        """
        path = self.images_fn[index]
        img = self.loader(path)
        imfullsize = max(img.size)

        if self.imsize is not None:
            img = imresize(img, self.imsize)

        if self.transform is not None:
            img = self.transform(img)
            
        img_idx = self.image_ids[index]
        valid = self.images[img_idx].point3D_ids > 0
        pt_ids = self.images[img_idx].point3D_ids[valid]
        pts = np.stack([self.points3d[i].xyz for i in pt_ids])
        mean, std = np.mean(pts, axis=0), np.std(pts, axis=0)
        if self.within_std > 0:
            valid_pts = np.all(np.abs(pts - mean) < self.within_std*std, axis=1)
            pts = pts[valid_pts]
        if self.demean:
            pts -= np.mean(pts, axis=0)
            pts /= np.std(pts, axis=0) 
        pts = torch.from_numpy(pts)
        
        if self.triplet: # return point cloud with little overlap for negative triplet loss
            if self.deterministic:
                random.seed(index)
            while True:
                j = random.randint(0, len(self.image_ids) - 1) # random.choice(enumerate(self.image_ids))
                trp_idx = self.image_ids[j]
                valid_trp = self.images[trp_idx].point3D_ids > 0
                pt_ids_trp = self.images[trp_idx].point3D_ids[valid_trp]
                shared = np.intersect1d(pt_ids, pt_ids_trp, assume_unique=True)
                if shared.shape[0] < pt_ids.shape[0] * 0.01:
                    break
            pts_trp = np.stack([self.points3d[i].xyz for i in pt_ids_trp])
            mean, std = np.mean(pts_trp, axis=0), np.std(pts_trp, axis=0)
            if self.within_std > 0:
                valid_pts = np.all(np.abs(pts_trp - mean) < self.within_std*std, axis=1)
                pts_trp = pts_trp[valid_pts]
            if self.demean:
                pts_trp -= np.mean(pts_trp, axis=0)
                pts_trp /= np.std(pts_trp, axis=0) 
            pts_trp = torch.from_numpy(pts_trp).float()
            path_trp = self.images_fn[j]
            img_trp = self.loader(path_trp)
            imfullsize_trp = max(img_trp.size)
            if self.imsize is not None:
                img_trp = imresize(img_trp, self.imsize)

            if self.transform is not None:
                img_trp = self.transform(img_trp)
        else:
            pts_trp = None
            img_trp = None

        return [img, pts.float(), img_trp, pts_trp] # {'anchor':img, 'pos':pts, 'neg':pts_trp}
    
    def __len__(self):
        return len(self.images_fn)


class ImagesFromList(data.Dataset):
    """A generic data loader that loads images from a list 
        (Based on ImageFolder from pytorch)

    Args:
        root (string): Root directory path.
        images (list): Relative image paths as strings.
        imsize (int, Default: None): Defines the maximum size of longer image side
        bbxs (list): List of (x1,y1,x2,y2) tuples to crop the query images
        transform (callable, optional): A function/transform that  takes in an PIL image
            and returns a transformed version. E.g, ``transforms.RandomCrop``
        loader (callable, optional): A function to load an image given its path.

     Attributes:
        images_fn (list): List of full image filename
    """

    def __init__(self, root, images, imsize=None, bbxs=None, transform=None, loader=default_loader):

        images_fn = [os.path.join(root,images[i]) for i in range(len(images))]

        if len(images_fn) == 0:
            raise(RuntimeError("Dataset contains 0 images!"))

        self.root = root
        self.images = images
        self.imsize = imsize
        self.images_fn = images_fn
        self.bbxs = bbxs
        self.transform = transform
        self.loader = loader

    def __getitem__(self, index):
        """
        Args:
            index (int): Index

        Returns:
            image (PIL): Loaded image
        """
        path = self.images_fn[index]
        img = self.loader(path)
        imfullsize = max(img.size)

        if self.bbxs is not None:
            img = img.crop(self.bbxs[index])

        if self.imsize is not None:
            if self.bbxs is not None:
                img = imresize(img, self.imsize * max(img.size) / imfullsize)
            else:
                img = imresize(img, self.imsize)

        if self.transform is not None:
            img = self.transform(img)

        return img

    def __len__(self):
        return len(self.images_fn)

    def __repr__(self):
        fmt_str = 'Dataset ' + self.__class__.__name__ + '\n'
        fmt_str += '    Number of images: {}\n'.format(self.__len__())
        fmt_str += '    Root Location: {}\n'.format(self.root)
        tmp = '    Transforms (if any): '
        fmt_str += '{0}{1}\n'.format(tmp, self.transform.__repr__().replace('\n', '\n' + ' ' * len(tmp)))
        return fmt_str

class ImagesFromDataList(data.Dataset):
    """A generic data loader that loads images given as an array of pytorch tensors
        (Based on ImageFolder from pytorch)

    Args:
        images (list): Images as tensors.
        transform (callable, optional): A function/transform that image as a tensors
            and returns a transformed version. E.g, ``normalize`` with mean and std
    """

    def __init__(self, images, transform=None):

        if len(images) == 0:
            raise(RuntimeError("Dataset contains 0 images!"))

        self.images = images
        self.transform = transform

    def __getitem__(self, index):
        """
        Args:
            index (int): Index

        Returns:
            image (Tensor): Loaded image
        """
        img = self.images[index]
        if self.transform is not None:
            img = self.transform(img)

        if len(img.size()):
            img = img.unsqueeze(0)

        return img

    def __len__(self):
        return len(self.images)

    def __repr__(self):
        fmt_str = 'Dataset ' + self.__class__.__name__ + '\n'
        fmt_str += '    Number of images: {}\n'.format(self.__len__())
        tmp = '    Transforms (if any): '
        fmt_str += '{0}{1}\n'.format(tmp, self.transform.__repr__().replace('\n', '\n' + ' ' * len(tmp)))
        return fmt_str