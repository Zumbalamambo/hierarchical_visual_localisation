"""
Author: A. Ziller (Technical University of Munich)

This script evaluates position and orientation of given images in a scene. 
It follows the hierarchical idea of hierarchical localization which is shortly summarized as follows:
1) Find global neighbors
2) Cluster global neighbors (optional)
3) Find local features
4) Match local features of query image and neighboring cluster
5) Calculate 6-DoF pose

Please see Note in README
"""

import argparse
import os
import numpy as np
import torch
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d import Axes3D
from torchvision import transforms
import cv2
import time
from collections import namedtuple
import sqlite3
from fnmatch import fnmatch
from pyquaternion import Quaternion
import warnings
import transforms3d.quaternions as txq

from common.feature_matching import LocalMatcher, GlobalMatcher, to_unit_vector
from dataset_loaders.txt_to_db import get_images, get_points
from dataset_loaders.utils import load_image
from dataset_loaders.pose_utils import quaternion_angular_error
import models.netvlad_vd16_pitts30k_conv5_3_max_dag as netvlad
import models.demo_superpoint as superpoint
from models.d2net.extract_features import d2net_interface
from models.cirtorch_network import init_network, extract_vectors


parser = argparse.ArgumentParser()
parser.add_argument('--global_method', default='Cirtorch', choices=['NetVLAD', 'Cirtorch'], help='Which method to use for global features')
parser.add_argument('--local_method', default='Colmap', choices=['Colmap', 'Superpoint', 'D2'], help='Which method to use for local features')
parser.add_argument('--colmap_query_database', default='data/queries.db', help='Database to colmap sift features if colmap is used as local method')
parser.add_argument('--database_path', default='data/AachenDayNight/aachen.db', help='Path to colmap database')
parser.add_argument('--global_features_db', default='data/global_features_low_res.db', help='Database for global features of database images')
parser.add_argument('--desc_database', default=None, help='If neural model (d2/superpoint) is used precalculated database descriptors speed everything up') 
parser.add_argument('--local_model_path', default='data/teacher_models/superpoint_v1.pth', help='Path to pretrained local descriptor model')
parser.add_argument('--nearest_method', default='approx', type = str, choices=['exact', 'LSH', 'approx'], help='Which method to use to find nearest global neighbors')
parser.add_argument('--local_matching_method', default='approx', type=str, choices=['exact', 'approx'], help='How local features are matched. Approx only considers direction of feature vector but is much faster.')
parser.add_argument('--global_resolution', default=224, type=int, help='Resolution on which nearest global neighbors are calculated')
parser.add_argument('--augmentation', action='store_true', help='Use augmented images')
parser.add_argument('--ratio_thresh', type=float, default=.75, help='Threshold for local feature matching in range [0.0, 1.0]. The higher it is the less similar matches have to be.')
parser.add_argument('--n_iter', type=int, default=5000, help='Number of iterations in RANSAC loop')
parser.add_argument('--reproj_error', type=float, default=8., help='Reprojection error of PnP-RANSAC loop')
parser.add_argument('--min_inliers', type=int, default=5, help='minimal number of inliers after PnP-RANSAC')
parser.add_argument('--n_neighbors', default=20, type=int, help='How many global neighbors are used')
parser.add_argument('--buckets', default=5, type=int, help='How many buckets are used for LSH hashing (Note: num of buckets = 2^(argument))')
parser.add_argument('--verify', default = None, choices=['all', 'day', 'night'], help='Use given dataset to evaluate error on own images. Verifies that pipeline works')
parser.add_argument('--dataset_dir', default='data/AachenDayNight/images_upright/query/', help='Dataset directory')
parser.add_argument('--overfit', default=None, type=int, help='Limit number of queries')
parser.add_argument('--out_file', type=str, default='aachen_eval_.txt', help='Name of output file')
parser.add_argument('--cluster', action='store_true', help='Create image cluster for all neighbors')
parser.add_argument('--no_refilter', action='store_false', help='Refilter local matches')
parser.add_argument('--bidirectional_filtering', action='store_true', help='Filter local matches in both directions')



## Taken from original hfnet repository
def qvec2rotmat(qvec):
    return np.array([
        [1 - 2 * qvec[2]**2 - 2 * qvec[3]**2,
         2 * qvec[1] * qvec[2] - 2 * qvec[0] * qvec[3],
         2 * qvec[3] * qvec[1] + 2 * qvec[0] * qvec[2]],
        [2 * qvec[1] * qvec[2] + 2 * qvec[0] * qvec[3],
         1 - 2 * qvec[1]**2 - 2 * qvec[3]**2,
         2 * qvec[2] * qvec[3] - 2 * qvec[0] * qvec[1]],
        [2 * qvec[3] * qvec[1] - 2 * qvec[0] * qvec[2],
         2 * qvec[2] * qvec[3] + 2 * qvec[0] * qvec[1],
        1 - 2 * qvec[1]**2 - 2 * qvec[2]**2]])

## Taken from original hfnet repository
def rotmat2qvec(R):
    Rxx, Ryx, Rzx, Rxy, Ryy, Rzy, Rxz, Ryz, Rzz = R.flat
    K = np.array([
        [Rxx - Ryy - Rzz, 0, 0, 0],
        [Ryx + Rxy, Ryy - Rxx - Rzz, 0, 0],
        [Rzx + Rxz, Rzy + Ryz, Rzz - Rxx - Ryy, 0],
        [Ryz - Rzy, Rzx - Rxz, Rxy - Ryx, Rxx + Ryy + Rzz]]) / 3.0
    eigvals, eigvecs = np.linalg.eigh(K)
    qvec = eigvecs[[3, 0, 1, 2], np.argmax(eigvals)]
    if qvec[0] < 0:
        qvec *= -1
    return qvec

## Taken from original hfnet repository
def colmap_image_to_pose(image):
    im_T_w = np.eye(4)
    im_T_w[:3, :3] = qvec2rotmat(image.qvec)
    im_T_w[:3, 3] = image.tvec
    w_T_im = np.linalg.inv(im_T_w)
    return w_T_im
def get_cursor(name):
    return sqlite3.connect(name).cursor()

## Taken from original hfnet repository
def descriptors_from_colmap_db(cursor, image_id):
    cursor.execute('SELECT cols, data FROM descriptors WHERE image_id=?;',(image_id,))
    feature_dim, blob = next(cursor)
    desc = np.frombuffer(blob, dtype=np.uint8).reshape(-1, feature_dim)
    return desc


## Taken from original hfnet repository
def keypoints_from_colmap_db(cursor, image_id):
    cursor.execute('SELECT cols, data FROM keypoints WHERE image_id=?;',(image_id,))
    cols, blob = next(cursor)
    kpts = np.frombuffer(blob, dtype=np.float32).reshape(-1, cols)[:, :2]
    return kpts

def get_kpts_desc(cursor, image_id):
    image_id = int(image_id)
    kpts = keypoints_from_colmap_db(cursor, image_id)[:, :2]
    desc = descriptors_from_colmap_db(cursor, image_id)
    return kpts, desc

## Taken from original hfnet repository
def get_img_id(cursor, img_name):
    img_id, = next(cursor.execute('SELECT image_id FROM images WHERE name=?;',(img_name,)))
    return img_id

def get_img_id_dataset(cursor, dataset_id):
    db_query_name = 'db/%d.jpg'%dataset.get_img_id(dataset_id)
    return get_img_id(cursor, db_query_name)

def kpts_to_cv(kpts, kpt_size=1.0):
    cv_kpts = []
    for i, kpt in enumerate(kpts):
        cv_kpts.append(cv2.KeyPoint(x=kpt[0], y=kpt[1], _size=kpt_size))
    return cv_kpts

def get_files(path, pattern, not_pattern = None, printout=False):
    found = []
    for path, subdirs, files in os.walk(path):
        for name in files:
            if fnmatch(name, pattern) and (not_pattern is None or not fnmatch(name, not_pattern)):
                found.append(os.path.join(path, name))
    if printout:
        print("Found %d files in path %s"%(len(found), path))
    return found


"""
Transforms errors into percentage format used in visuallocalization.net
"""
def percentage_stats(errors_trans, errors_rot, day=True):
    num_high, num_medium, num_coarse = 0,0,0
    for t, q in zip(errors_trans, errors_rot):
        if day and t <= 0.25 and q <= 2.0:
            num_high += 1
        elif not day and t <= 0.5 and q <= 2.0:
            num_high += 1
        if day and t <= 0.5 and q <= 5.0:
            num_medium += 1
        elif not day and t <= 1.0 and q <= 5.0:
            num_medium += 1
        if t <= 5.0 and q <= 10.0:
            num_coarse += 1
    per_high = float(num_high)/float(len(errors_trans))*100.0
    per_medium = float(num_medium)/float(len(errors_trans))*100.0
    per_coarse = float(num_coarse)/float(len(errors_trans))*100.0
    return (per_high, per_medium, per_coarse)

"""
Turns seconds into human readable time string
"""
def time_to_str(t):
    out_str = ''
    if t > 86400:
        out_str += '%d days '%(t//86400)
        t %= 86400
    if t > 3600:
        out_str += '%d hours '%(t//3600)
        t %= 3600
    if t > 60:
        out_str += '%d minutes '%(t//60)
        t %= 60
    out_str += '%.1f seconds'%t
        
    return out_str

"""
Use for colour output
"""
class bcolors:
    HEADER = '\033[95m'
    OKBLUE = '\033[94m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'

"""
Check in verification pipeline if global neighbor has matching 3d points with query image
"""
def calc_neighbor_match(img_idx, neighbor_idx, images):
    oimg = images[img_idx]
    nimg = images[neighbor_idx]

    valid_o = oimg.point3D_ids > 0 
    pt_ids_o = oimg.point3D_ids[valid_o]

    valid_n = nimg.point3D_ids > 0 
    pt_ids_n = nimg.point3D_ids[valid_n]

    shared = np.isin(pt_ids_o, pt_ids_n)
    pt_ids_s = pt_ids_o[shared]
    #print('Images match {:.1f}%'.format(100.0*(pt_ids_s.shape[0]/pt_ids_o.shape[0])))
    return 100.0*(pt_ids_s.shape[0]/min([pt_ids_o.shape[0], pt_ids_n.shape[0]]))

def get_camera_matrices():
    camera_matrices = {}
    query_intrinsics_files = ['data/AachenDayNight/queries/day_time_queries_with_intrinsics.txt',
                             'data/AachenDayNight/queries/night_time_queries_with_intrinsics.txt',
                             'data/AachenDayNight/database_intrinsics.txt']
    for file_path in query_intrinsics_files:
        with open(file_path, 'r') as f:
            lines = [l.strip() for l in f.readlines()]
            for line in lines:
                # Format: `image_name SIMPLE_RADIAL w h f cx cy r`
                line = line.split(' ')
                img_path = line[0]
                f = float(line[4])
                cx = float(line[5])
                cy = float(line[6])
                rad_dist = float(line[7])
                A = np.array([[f, 0, cx],[0, f, cy], [0, 0, 1]])
                camera_matrices[img_path] = {'cameraMatrix': A, 'rad_dist':rad_dist}
    return camera_matrices

def get_img_cluster(images, points3d):
    img_cluster = {img: set() for img in images.keys()} 
    for p_id in points3d.keys(): 
        img_ids = set(points3d[p_id].image_ids)
        for img_id in img_ids:
            img_cluster[img_id] |= img_ids
    return img_cluster

def double_matching(local_matcher, query_desc, neighbor_desc):
    matches_forward = local_matcher.match(query_desc, neighbor_desc)
    matches_reverse = local_matcher.match(neighbor_desc, query_desc)
    if matches_forward.shape[0] == 0 or matches_reverse.shape[0] == 0:
        return np.array([])
    #print(matches_forward.shape)
    #print(matches_reverse.shape)
    matches = []
    mr = list(matches_reverse[:,1])
    for m in matches_forward:
        if m[0] in mr:
            matches.append(m)
    matches = np.array(matches)
    return matches

def match_local(args, mm, query_desc, query_kpts, images, points3d, query_id, cluster_query, database_cursor, model, matcher, refilter=False, double=False, desc_database=None):
    cuda = torch.cuda.is_available()
    matched_kpts_cv = []
    matched_pts = []
    #matcher = cv2.BFMatcher.create(cv2.NORM_L2)
    data_descs = []
    if refilter:
        pt_ids_all = []
        data_descs = []
    if 'approx' in mm:
        query_desc = to_unit_vector(query_desc, method=mm, cuda=cuda)
    correct, incorrect = 0, 0
    augments = 0
    if args.verify is not None and args.local_method == 'Colmap':
        valid_o = images[query_image_ids[query_id]].point3D_ids > 0 
        pt_ids_o = images[query_image_ids[query_id]].point3D_ids
        pt_ids_o = pt_ids_o[:pt_ids_o.shape[0]//2]
    for c in cluster_query:
        for img in c:
            if args.verify is not None and abs(img) == abs(query_image_ids[query_id]):
                continue
            img_name = images[abs(img)].name
            valid = images[abs(img)].point3D_ids > 0 
            pt_ids = images[abs(img)].point3D_ids[valid]
            if args.local_method == 'Colmap':
                data_desc = descriptors_from_colmap_db(database_cursor, int(img))
                #data_kpts = kpts_to_cv(data_kpts[valid[:data_kpts.shape[0]]] - 0.5)
                data_desc = data_desc[valid[:data_desc.shape[0]]]
            elif args.local_method in ['Superpoint', 'D2']:
                if img > 0:
                    path_to_img = 'data/AachenDayNight/images_upright/'+img_name
                else:
                    path_to_img = 'data/AachenDayNight/AugmentedNightImages_high_res/'+img_name.replace('db/', '').replace('.jpg', '.png')
                    augments += 1
                if desc_database is None:
                    data_kpts = keypoints_from_colmap_db(database_cursor, abs(int(img)))
                    data_kpts = data_kpts[valid[:data_kpts.shape[0]]] - 0.5
                    if args.local_method == 'Superpoint':
                        cv_img = cv2.imread(path_to_img, 0).astype(np.float32)/255.0
                        _, data_desc, _ = model.run(cv_img, points=data_kpts)
                        data_desc = data_desc.T
                    elif args.local_method == 'D2':
                        fixed_kpts = np.flip(data_kpts.copy(), axis=1)
                        #print(fixed_kpts.shape)
                        #print(fixed_kpts.max(axis=0))
                        data_desc = model.get_features(path_to_img, fixed_kpts)
                else:
                    desc_database.execute('SELECT cols, desc FROM local_features WHERE image_id=?;', (abs(int(img)),))
                    c, d = next(desc_database)
                    data_desc = np.frombuffer(d, dtype=np.float32).reshape(c, -1)
                    
                        
                #print('Query desc shape: {} \t Data desc shape: {}'.format(query_desc.shape, data_desc.shape))
                ##database version
                #superpoint_cursor.execute('SELECT cols, desc FROM local_features WHERE image_id==?;',(int(img),))
                #cols, desc = next(superpoint_cursor)
                #data_desc = np.frombuffer(desc, dtype=np.float32).reshape(cols, 256)
            if 'approx' in mm:
                data_desc = to_unit_vector(data_desc, method=mm, cuda=cuda)
            if double:
                matches = double_matching(matcher, query_desc, data_desc)
            else:
                matches = matcher.match(query_desc, data_desc)
            #print('Found {} matches'.format(matches.shape[0]))
            if type(data_desc) is torch.Tensor:
                data_desc = data_desc.cpu().numpy()
            if matches.shape[0] > 0:
                if refilter:
                    pt_ids_all.append(pt_ids[matches[:,1]])
                    data_descs.append(data_desc[matches[:,1]])
                else:
                    matched_kpts_cv += [query_kpts[m[0]] for m in matches]
                    matched_pts += [pt_ids[m[1]] for m in matches]
    if refilter:
        if len(pt_ids_all) < 1 or (len(data_descs) < 2):
            matches = np.array([])
        else:
            pt_ids_all = np.concatenate(pt_ids_all)
            data_descs = np.vstack(data_descs)
            if 'approx' in mm:
                data_descs = to_unit_vector(data_descs, method=mm, cuda=cuda)
            
            if double:
                matches = double_matching(matcher, query_desc, data_descs)
            else:
                matches = matcher.match(query_desc, data_descs)
            matched_kpts_cv = [query_kpts[m[0]] for m in matches]
            matched_pts = [pt_ids_all[m[1]] for m in matches]
                    
            if args.verify is not None and args.local_method == 'Colmap':
                for m1, m2 in matches:
                    if valid_o[m1]:
                        if pt_ids_o[m1] == pt_ids_all[m2]:
                            correct += 1
                        else:
                            incorrect += 1
    if len(matched_pts) > 0:
        matched_pts_xyz = np.stack([points3d[i].xyz for i in matched_pts])
        matched_keypoints = np.vstack([np.array([x.pt[0], x.pt[1]]) for x in matched_kpts_cv])
    else:
        matched_pts_xyz = np.array([])
        matched_keypoints = np.array([])
    if args.augmentation:
        print_column_entry(' - Augmented images used', augments)
    return matched_pts_xyz, matched_keypoints, correct, incorrect



"""
Output helpers
"""
column_indents = [40, 1]
seperating_char = '| '
def print_column_entry(left_column, right_column, indents=column_indents, seperating_char=seperating_char):
    print('\t{:{}} {:>{}}{}'.format(left_column, indents[0], seperating_char, indents[1], right_column))

sep_length = 100
def print_seperator():
    print('-'*sep_length)
    
def print_stats(li, lic, lii, errors, errors_rot, inlier_nums, inlier_rates, nbs, name):
    if np.any(li):
        print_seperator()
        print_column_entry(name, '{} images'.format(np.sum(li)))
        print_column_entry('Total / Correct / Incorrect', '{:d}\t / \t{:d}\t / \t{:d}'.format(np.sum(li), np.sum(lic), np.sum(lii)))
        print_column_entry('Mean translational error', '{:.4f} m\t / \t{:.4f} m\t / \t{:.4f} m'.format(np.mean(errors[li]), np.mean(errors[lic]) if np.any(lic) else float('nan'), np.mean(errors[lii]) if np.any(lii) else float('nan')))
        print_column_entry('Median translational error', '{:.4f} m\t / \t{:.4f} m\t / \t{:.4f} m'.format(np.median(errors[li]), np.median(errors[lic]) if np.any(lic) else float('nan'), np.median(errors[lii]) if np.any(lii) else float('nan')))
        print_column_entry('Max translational error', '{:.4f} m\t / \t{:.4f} m\t / \t{:.4f} m'.format(np.max(errors[li]), np.max(errors[lic]) if np.any(lic) else float('nan'), np.max(errors[lii]) if np.any(lii) else float('nan')))
        print_column_entry('Mean angular error', '{:.4f} °\t / \t{:.4f} °\t / \t{:.4f} °'.format(np.mean(errors_rot[li]), np.mean(errors_rot[lic]) if np.any(lic) else float('nan'), np.mean(errors_rot[lii]) if np.any(lii) else float('nan')))
        print_column_entry('Median angular error', '{:.4f} °\t / \t{:.4f} °\t / \t{:.4f} °'.format(np.median(errors_rot[li]), np.median(errors_rot[lic]) if np.any(lic) else float('nan'), np.median(errors_rot[lii]) if np.any(lii) else float('nan')))
        print_column_entry('Max angular error', '{:.4f} °\t / \t{:.4f} °\t / \t{:.4f} °'.format(np.max(errors_rot[li]), np.max(errors_rot[lic]) if np.any(lic) else float('nan'), np.max(errors_rot[lii]) if np.any(lii) else float('nan')))
        print_column_entry('Average inlier rate', '({:.1f}% / {:.1f}) / ({:.1f}% / {:.1f}) / ({:.1f}% / {:.1f})'.format(np.mean(inlier_rates[li]), np.mean(inlier_nums[li]), np.mean(inlier_rates[lic]) if np.any(lic) else float('nan'), np.mean(inlier_nums[lic]) if np.any(lic) else float('nan'), np.mean(inlier_rates[lii]) if np.any(lii) else float('nan'), np.mean(inlier_nums[lii]) if np.any(lii) else float('nan')))
        print_column_entry('Min inlier rate', '({:.1f}% / {:.0f}) / ({:.1f}% / {:.0f}) / ({:.1f}% / {:.0f})'.format(np.min(inlier_rates[li]), np.min(inlier_nums[li]), np.min(inlier_rates[lic]) if np.any(lic) else float('nan'), np.min(inlier_nums[lic]) if np.any(lic) else float('nan'), np.min(inlier_rates[lii]) if np.any(lii) else float('nan'), np.min(inlier_nums[lii]) if np.any(lii) else float('nan')))
        print_column_entry('Max inlier rate', '({:.1f}% / {:.0f}) / ({:.1f}% / {:.0f}) / ({:.1f}% / {:.0f})'.format(np.max(inlier_rates[li]), np.max(inlier_nums[li]), np.max(inlier_rates[lic]) if np.any(lic) else float('nan'), np.max(inlier_nums[lic]) if np.any(lic) else float('nan'), np.max(inlier_rates[lii]) if np.any(lii) else float('nan'), np.max(inlier_nums[lii]) if np.any(lii) else float('nan')))
        print_column_entry('Average correct neighbors', '{:.1f}\t / \t {:.1f}\t / \t {:.1f}'.format(np.mean(nbs[li]), np.mean(nbs[lic]) if np.any(lic) else float('nan'), np.mean(nbs[lii]) if np.any(lii) else float('nan')))
        print_column_entry('Min correct neighbors', '{:.0f}\t / \t {:.0f}\t / \t {:.0f}'.format(np.min(nbs[li]), np.min(nbs[lic]) if np.any(lic) else float('nan'), np.min(nbs[lii]) if np.any(lii) else float('nan')))
        print_column_entry('Max correct neighbors', '{:.0f}\t / \t {:.0f}\t / \t {:.0f}'.format(np.max(nbs[li]), np.max(nbs[lic]) if np.any(lic) else float('nan'), np.max(nbs[lii]) if np.any(lii) else float('nan')))


"""
print config given by command line arguments
"""
def print_config(args):
    print('Configuration')
    print_column_entry('Evaluation image directory', args.dataset_dir)
    print_column_entry('Global method', args.global_method)
    if args.global_method == 'NetVLAD':
        print_column_entry('Global resolution', args.global_resolution)
    print_column_entry('Local method', args.local_method)
    if args.local_method in ['Superpoint', 'D2']:
        #print_column_entry(' - Database', args.superpoint_database)
        print_column_entry(' - Model', args.local_model_path)
    print_column_entry('Nearest neighbor method', args.nearest_method)
    if args.nearest_method == 'LSH':
        print_column_entry(' - hash buckets', 2**args.buckets)
    print_column_entry('k neighbors', args.n_neighbors)
    print_column_entry('Do clustering', args.cluster)
    print_column_entry('Local matching method', args.local_matching_method)
    print_column_entry('Refilter', args.no_refilter)
    print_column_entry('Bidirectional filtering', args.bidirectional_filtering)
    print_column_entry('Matching threshold', args.ratio_thresh)
    print_column_entry('Num iterations RANSAC', args.n_iter)
    print_column_entry('Reprojection error', args.reproj_error)
    print_column_entry('Minimum num inliers PnP', args.min_inliers)
    print_column_entry('Use augmentation', args.augmentation)
    if args.augmentation and (args.local_method == 'Colmap' or args.global_method == 'NetVLAD'):
        raise NotImplementedError('Augmentation currently only works for Cirtorch/Superpoint')



"""
Loading data from storage into memory
"""
def setup(args):
    print('Setup')
    setup_time = time.time()
    t = time.time()
    images = get_images()
    points3d = get_points()
    t = time.time() - t
    print_column_entry('Read {} images and {} 3d points'.format(len(images), len(points3d)), time_to_str(t))
    #get_img = lambda i: np.array(load_image('data/AachenDayNight/images_upright/'+images[i].name))
    database_cursor = get_cursor(args.database_path)
    if args.local_method == 'Colmap':
        query_cursor = get_cursor(args.colmap_query_database)
    else:
        query_cursor = None
    ##create image clusters
    if args.cluster:
        t = time.time()
        img_cluster = get_img_cluster(images, points3d)
        t = time.time() - t
        print_column_entry('Found {} cluster'.format(len(img_cluster)), time_to_str(t))
    else:
        img_cluster = None
    # Camera matrix
    t = time.time()
    camera_matrices = get_camera_matrices()
    t = time.time() - t
    print_column_entry('Read camera matrices', time_to_str(t))

    t = time.time()
    if args.verify is not None:
        query_images = []
        query_image_ids = []
        if args.verify == 'all' or args.verify == 'day':
            query_images += [os.path.join(args.dataset_dir, images[i].name) for i in images]
            query_image_ids += [i for i in images]
        if args.verify == 'all' or args.verify == 'night':
            query_images += [os.path.join('data/AachenDayNight/AugmentedNightImages_high_res', images[i].name.replace('db/', '').replace('.jpg', '.png')) for i in images]
            query_image_ids += [-i for i in images]
    else:
        query_images = get_files(args.dataset_dir, '*.jpg')
        query_image_ids = None
    if args.overfit is not None:
        query_images = query_images[:args.overfit]
    t = time.time() - t
    setup_time = time.time() - setup_time
    
    print_column_entry('Found {} query images'.format(len(query_images)), time_to_str(t))
    print_column_entry('Total time', time_to_str(setup_time))
    
    return points3d, images, database_cursor, query_cursor, img_cluster, camera_matrices, query_images, query_image_ids, setup_time


"""
Finds globally similar images for each query
"""
def global_neighbors(args, query_images):
    print('Global Neighbors')

    global_time = time.time()
    t = time.time()
    print_column_entry('Calculating query descriptors', '')
    if args.global_method == 'NetVLAD':
        model = netvlad.vd16_pitts30k_conv5_3_max_dag(weights_path='data/teacher_models/netvlad_pytorch/vd16_pitts30k_conv5_3_max_dag.pth')
        model.eval()
        query_global_desc = []
        CUDA = torch.cuda.is_available()
        if CUDA:
            model = model.cuda()
        low_res_transform = transforms.Compose([transforms.Resize(224),#args.global_resolution),
                                                transforms.CenterCrop(args.global_resolution), transforms.ToTensor() ])
        for cnt, img in enumerate(query_images):
            if cnt % (len(query_images)//5) == 0:
                print_column_entry('', '{}/{} query descriptors'.format(cnt, len(query_images)))
            if CUDA:
                 query_global_desc.append(model(low_res_transform(load_image(img)).cuda().unsqueeze(0)).detach().cpu().squeeze(0).numpy())
            else:
                query_global_desc.append(model(low_res_transform(load_image(img)).unsqueeze(0)).detach().cpu().squeeze(0).numpy())
        query_global_desc = np.vstack(query_global_desc)
    elif args.global_method == 'Cirtorch':
        state = torch.load('data/teacher_models/retrievalSfM120k-resnet101-gem-b80fb85.pth')   
        net_params = {}
        net_params['architecture'] = state['meta']['architecture']
        net_params['pooling'] = state['meta']['pooling']
        net_params['local_whitening'] = state['meta'].get('local_whitening', False)
        net_params['regional'] = state['meta'].get('regional', False)
        net_params['whitening'] = state['meta'].get('whitening', False)
        net_params['mean'] = state['meta']['mean']
        net_params['std'] = state['meta']['std']
        net_params['pretrained'] = False
        # load network
        net = init_network(net_params)
        net.load_state_dict(state['state_dict'])
        if 'Lw' in state['meta']:
            net.meta['Lw'] = state['meta']['Lw']
        ms = list(eval('[1]'))
        if len(ms)>1 and net.meta['pooling'] == 'gem' and not net.meta['regional'] and not net.meta['whitening']:
            msp = net.pool.p.item()
            print(">> Set-up multiscale:")
            print(">>>> ms: {}".format(ms))            
            print(">>>> msp: {}".format(msp))
        else:
            msp = 1
        if torch.cuda.is_available():
            net.cuda()
        net.eval()
        # set up the transform
        normalize = transforms.Normalize(
            mean=net.meta['mean'],
            std=net.meta['std']
        )
        transform = transforms.Compose([
            transforms.ToTensor(),
            normalize
        ])
        Lw = None
        query_global_desc = extract_vectors(net, query_images, 1024, transform, ms=ms, msp=msp)
        query_global_desc = query_global_desc.numpy().T
    else:
        raise NotImplementedError('Global method not implemented')
    t = time.time() - t

    print_column_entry('{}-dim global query desc'.format(query_global_desc.shape), time_to_str(t))

    t = time.time()
    if args.global_method == 'NetVLAD':
        global_features_cursor = get_cursor(args.global_features_db)
        global_features = []
        image_ids = []
        for row in global_features_cursor.execute('SELECT image_id, cols, data FROM global_features;'):
            global_features.append(np.frombuffer(row[2], dtype=np.float32).reshape(-1, row[1]))
            image_ids.append(row[0])
        global_features = np.vstack(global_features)
        global_features_cursor.close()
    elif args.global_method == 'Cirtorch':
        global_features = np.load('data/cirtorch_data_descs.npy').T
        image_ids = [images[i].id for i in images]
        if args.augmentation:
            aug_features = np.load('data/cirtorch_augmented_descs.npy').T
            global_features = np.concatenate([global_features, aug_features])
            image_ids += [-images[i].id for i in images]
    t = time.time() - t
    print_column_entry('Database global features loaded', time_to_str(t))

    
    """
    Match query to database descriptors
    """
    t = time.time()
    n_neighbors = args.n_neighbors
    if args.verify is not None:
        n_neighbors+= 2 if args.augmentation else 1
    Matcher = GlobalMatcher(args.nearest_method, n_neighbors, False, args.buckets)
    indices = Matcher.match(global_features, query_global_desc)
    """
    For verification pipeline the closest neighbor is always the query image itself.
    Hence we remove it to simulate real conditions.
    """
    if args.verify is not None:
        indices_cut = []
        for i in range(indices.shape[0]):
            #indices_cut.append(indices[i,1:n_images+1])
            lst = []
            j = 0
            img_itself = abs(image_ids[indices[i,0]])
            for idx in indices[i,1:]:
                if abs(image_ids[idx]) != img_itself:
                    lst.append(idx)
                    j += 1
                if j >= args.n_neighbors:
                    break
            indices_cut.append(lst)
            #print(len(lst))
        indices = np.stack(indices_cut)
    else:
        indices = indices[:,:n_neighbors]
    t = time.time() - t
    print_column_entry('Nearest neighbors for all queries', time_to_str(t))

    return indices, image_ids


"""
Matches local features of query to cluster images and calculates 6dof pose
"""
def local_matching(args, points3d, images, database_cursor, query_cursor, img_cluster, camera_matrices, query_images, query_image_ids, indices, image_ids, out_file):
    ## Local features matching and pose retrieval
    model = None
    if args.local_method == 'Superpoint':
        model = superpoint.SuperPointFrontend(weights_path=args.local_model_path,nms_dist=4, conf_thresh=0.015, nn_thresh=.7, cuda=torch.cuda.is_available())
    elif args.local_method == 'D2':
        model = d2net_interface(model_file=args.local_model_path, use_relu=False)
    image_times = []
    top_neighbor_match = []
    local_matching_rate = []
    inlier_rates, inlier_nums = [], []
    errors = []
    errors_rot = []
    mm = 'OpenCV' if args.local_matching_method == 'exact' else ('approx_torch' if torch.cuda.is_available() else 'approx_numpy')
    cuda = torch.cuda.is_available()
    matcher = LocalMatcher(args.ratio_thresh, mm, True)
    desc_database_cursor = get_cursor(args.desc_database) if args.desc_database is not None else None
    print('Local feature matching and pose retrieval')
    
    backfall_cm = [camera_matrices[i]['cameraMatrix'] for i in camera_matrices]
    backfall_cm = np.stack(backfall_cm)
    backfall_cm = np.median(backfall_cm, axis=0)
    
    for query_id, query_name in enumerate(query_images):

        ## Make sure we have camera parameters.
        if args.verify is not None:
            query_path = images[abs(query_image_ids[query_id])].name
        else:
            query_path = os.path.join(*os.path.normpath(query_name).split(os.sep)[-4:])
            

        individual_image_time = time.time()
        print_column_entry('Processing query image {}/{}'.format(query_id+1, len(query_images)), 'Expected remaining time: {}'.format(time_to_str(np.mean(image_times)*(len(query_images)-query_id))) if query_id > 0 else '')
        print_column_entry('Query path', query_name)
        if args.verify is not None:
            tn = []
            for i, idx in enumerate(indices[query_id]):
                global_match = calc_neighbor_match(abs(query_image_ids[query_id]), abs(image_ids[idx]), images)
                tn.append(global_match)
                #print_column_entry(' - Neighbor {} match'.format(i+1), '{:.1f}%'.format(global_match))
            print_column_entry(' - # Neighbors with match > 0', '{}'.format(len([i for i in tn if i > 0.0])))
            top_neighbor_match.append(tn)
        t = time.time()
        cluster_orig_ids = [image_ids[indices[query_id][0]]]
        if args.cluster:
            cluster_query = [img_cluster[i] for i in cluster_orig_ids]
            for i, ind in enumerate(indices[query_id]):
                ind = image_ids[ind]
                if i == 0:
                    continue
                point_set = img_cluster[ind]
                disjoint = False
                for j, c in enumerate(cluster_query):
                    if ind in c:
                        cluster_query[j] |= point_set
                        disjoint = True
                        break
                if not disjoint:
                    cluster_orig_ids.append(ind)
                    cluster_query.append(point_set)
        else:
            cluster_query = [[image_ids[indices[query_id][i]] for i in range(args.n_neighbors)]]
        t = time.time() - t
        #print_column_entry('Global neighbor ids', str(cluster_query))
        print_column_entry(' - Clustered neighbors', time_to_str(t))


        ## Local features
        t = time.time()
        if args.local_method == 'Colmap':
            ## query desc
            test_query_path = query_name.replace(args.dataset_dir, '')
            query_img_id = get_img_id(query_cursor, test_query_path)
            query_kpts, query_desc = get_kpts_desc(query_cursor, query_img_id)
            query_kpts = kpts_to_cv(query_kpts)
        elif args.local_method == 'Superpoint':
            cv_img = cv2.imread(query_name, 0).astype(np.float32)/255.0
            kpts, query_desc, _ = model.run(cv_img)
            query_desc = query_desc.T
            query_kpts = kpts_to_cv(kpts.T)
        elif args.local_method == 'D2':
            query_kpts, query_desc, _ =  model.extract_features(query_name, only_path=True)
            #if 'ots' in args.local_model_path or 'no_photo' in args.local_model_path:
            query_kpts = query_kpts[0]
            query_desc = query_desc[0]
            query_kpts = query_kpts[:,0:2]
            query_kpts = kpts_to_cv(query_kpts)
        else:
            raise NotImplementedError('Local feature extraction method not implemented')
        t = time.time() - t
        print_column_entry(' - Got query keypoints and descriptors', time_to_str(t))


        ## Matching
        t = time.time()
        matched_pts_xyz, matched_keypoints, correct, incorrect = match_local(args, mm, query_desc, query_kpts, images, points3d, query_id, cluster_query, database_cursor, model, matcher, refilter=args.no_refilter, double=args.bidirectional_filtering, desc_database=desc_database_cursor)
        t = time.time() - t
        print_column_entry(' - Number of matched points', matched_keypoints.shape[0])
        
        #if len(matched_keypoints) < 5:
        #    warnings.warn('Number of matched points too little. Lowering matching threshold recommended.')
        #    continue
        if args.verify is not None and args.local_method == 'Colmap':
            sci = correct+incorrect
            correct_prct = 100.0*(correct/float(sci)) if sci > 0 else 0.0
            local_matching_rate.append(correct_prct)
            print_column_entry(' - Correctly matched', '{:.1f}%'.format(correct_prct))
        print_column_entry(' - Finished matching', time_to_str(t))
        


        ## Calculate pose
        t = time.time()
        if 'Augmented' in query_path:
            query_path = query_path.replace('data/AachenDayNight/AugmentedNightImages_high_res/', 'db/').replace('.png', '.jpg')
        if query_path not in camera_matrices:
            print_column_entry('WARNING', '--CAMERA MATRIX UNKOWN -- USE BACKFALL --')
            camera_matrix = backfall_cm
        else:
            cm = camera_matrices[query_path]
            camera_matrix = cm['cameraMatrix']
        distortion_coeff = cm['rad_dist']
        dist_vec = np.array([distortion_coeff, 0, 0, 0])
        
        if matched_pts_xyz.shape[0] > 4:
            success, R_vec, translation, inliers = cv2.solvePnPRansac(
                matched_pts_xyz, matched_keypoints, camera_matrix, dist_vec,
                iterationsCount=args.n_iter, reprojectionError=args.reproj_error,
                flags=cv2.SOLVEPNP_P3P)
        else:
            inliers = None
            success = False

        if inliers is not None:
            inliers = inliers[:, 0] if len(inliers.shape) > 1 else inliers
            num_inliers = len(inliers)
            inlier_ratio = len(inliers) / len(matched_keypoints)
        else:
            num_inliers = 0
            inlier_ratio = 0
        inlier_rates.append(100.0*inlier_ratio)
        inlier_nums.append(num_inliers)
        print_column_entry(' - Inlier ratio', '{:.1f}%'.format(100.0*inlier_ratio))
        print_column_entry(' - Inliers total', num_inliers)
        success &= num_inliers >= args.min_inliers
        #if inlier_ratio < 0.05:
        #    warnings.warn('Very low inlier ratio')

        if success:
            ret, R_vec, t = cv2.solvePnP(
                        matched_pts_xyz[inliers], matched_keypoints[inliers], camera_matrix,
                        dist_vec, rvec=R_vec, tvec=translation, useExtrinsicGuess=True,
                        flags=cv2.SOLVEPNP_ITERATIVE)
            success &= ret

            query_T_w = np.eye(4)
            query_T_w[:3, :3] = cv2.Rodrigues(R_vec)[0]
            query_T_w[:3, 3] = t[:, 0]
            w_T_query = np.linalg.inv(query_T_w)

            name = os.path.split(query_name)[-1]
            position = w_T_query[:3, 3]
            quat = list(Quaternion(matrix=query_T_w)) # rotmat2qvec(w_T_query[:3,:3])
            print_column_entry(' - Calculated position', position)
        #else:
            #if not success:
            #    warnings.warn('Localization not successful!')
        #    errors.append(1000)    ## can be chosen arbitrarily
        #    errors_rot.append(180) ## same here

        if args.verify is not None:
            gt = colmap_image_to_pose(images[abs(query_image_ids[query_id])])[:3,3]
            rotation = images[abs(query_image_ids[query_id])].qvec
            error_rot = quaternion_angular_error(rotation, quat)
            error = np.linalg.norm(position-gt)
            error_str = '%.1f m'%error if error > 1e-1 else '%.1f cm'%(100.0*error)
            errors.append(error)
            errors_rot.append(error_rot)
            print_column_entry(' - Groundtruth', gt)
            if error > 5.0 or error_rot > 10.0:
                print(bcolors.FAIL, end='')
            print_column_entry(' - Translation error', error_str)
            print_column_entry(' - Angular error', '{:.2f}°'.format(error_rot))
            if error > 5.0 or error_rot > 10.0:
                print(bcolors.ENDC, end='')

            #out_file.write('{} Error: {} CalcPos: {}\n'.format(name, error, position))
        else:
            if success:
                position = -txq.rotate_vector(np.array(position), np.array(quat))
                out_file.write('{} {} {} {} {} {} {} {}\n'.format(name, quat[0], quat[1], quat[2], quat[3], position[0], position[1], position[2]))

        individual_image_time = time.time() - individual_image_time 
        print_column_entry('Finished image {}/{}'.format(query_id+1, len(query_images)), time_to_str(individual_image_time))
        print_seperator()
        image_times.append(individual_image_time)
    return image_times, np.array(errors), np.array(errors_rot), np.array(top_neighbor_match), np.array(local_matching_rate), np.array(inlier_rates), np.array(inlier_nums)



def stats(args, setup_time, image_times, errors, errors_rot, out_file, top_neighbor_match, local_matching_rate, inlier_rates, inlier_nums):
    print('Stats')
    print_column_entry('Setup time', time_to_str(setup_time))
    print_column_entry('Average time per image', time_to_str(np.mean(image_times)))
    print_column_entry('Median time per image', time_to_str(np.median(image_times)))
    print_column_entry('Max image time', time_to_str(np.max(image_times)))
    print_seperator()
    if args.verify is not None:
        out_file.write(str(args))
        out_file.write('\n')
        
        
        print_column_entry('Mean translational error', '{:.4f} m'.format(np.mean(errors)))
        print_column_entry('Median translational error', '{:.4f} m'.format(np.median(errors)))
        print_column_entry('Max translational error', '{:.4f} m'.format(np.max(errors)))
        print_column_entry('Mean angular error', '{:.4f} °'.format(np.mean(errors_rot)))
        print_column_entry('Median angular error', '{:.4f} °'.format(np.median(errors_rot)))
        print_column_entry('Max angular error', '{:.4f} °'.format(np.max(errors_rot)))
        if args.local_method == 'Colmap':
            print_column_entry('Average local matching rate', '{:.1f}%'.format(np.mean(local_matching_rate)))
        print_column_entry('Average inlier rate', '{:.1f}% / Total average: {}'.format(np.mean(inlier_rates), np.mean(inlier_nums)))
        print_column_entry('Min inlier rate', '{:.1f}% / Total min: {}'.format(np.min(inlier_rates), np.min(inlier_nums)))
        print_column_entry('Max inlier rate', '{:.1f}% / Total max: {}'.format(np.max(inlier_rates), np.max(inlier_nums)))
        print_column_entry('Percentage results', '{:.1f} / {:.1f} / {:.1f}'.format(*percentage_stats(errors, errors_rot)))
        

        out_file.write('Mean translational error\t{:.4f} m\n'.format(np.mean(errors)))
        out_file.write('Median translational error\t{:.4f} m\n'.format(np.median(errors)))
        out_file.write('Max translational error\t{:.4f} m\n'.format(np.max(errors)))
        out_file.write('Mean angular error\t{:.4f} °\n'.format(np.mean(errors_rot)))
        out_file.write('Median angular error\t{:.4f} °\n'.format(np.median(errors_rot)))
        out_file.write('Max angular error\t{:.4f} °\n'.format(np.max(errors_rot)))
        if args.local_method == 'Colmap':
            out_file.write('Average local matching rate\t{:.1f}%\n'.format(np.mean(local_matching_rate)))
        out_file.write('Average inlier rate\t{:.1f}% / Total average: {}\n'.format(np.mean(inlier_rates), np.mean(inlier_nums)))
        out_file.write('Min inlier rate\t{:.1f}% / Total min: {}\n'.format(np.min(inlier_rates), np.min(inlier_nums)))
        out_file.write('Max inlier rate\t{:.1f}% / Total max: {}\n'.format(np.max(inlier_rates), np.max(inlier_nums)))
        out_file.write('Percentage results\t{:.1f} / {:.1f} / {:.1f}\n'.format(*percentage_stats(errors, errors_rot)))
        
        if top_neighbor_match.max(axis=1).shape[0] > 0:
            valid = top_neighbor_match.max(axis=1) > 0.0
            print_seperator()
            if np.any(valid):
                print_column_entry('Filtered by good neighbors', '{} images'.format(errors[valid].shape[0]))
                print_column_entry('Mean translational error', '{:.4f} m'.format(np.mean(errors[valid])))
                print_column_entry('Median translational error', '{:.4f} m'.format(np.median(errors[valid])))
                print_column_entry('Max translational error', '{:.4f} m'.format(np.max(errors[valid])))
                print_column_entry('Mean angular error', '{:.4f} °'.format(np.mean(errors_rot[valid])))
                print_column_entry('Median angular error', '{:.4f} °'.format(np.median(errors_rot[valid])))
                print_column_entry('Max angular error', '{:.4f} °'.format(np.max(errors_rot[valid])))
                if args.local_method == 'Colmap':
                    print_column_entry('Average local matching rate', '{:.1f}%'.format(np.mean(local_matching_rate[valid])))
                print_column_entry('Average inlier rate', '{:.1f}% / Total average: {}'.format(np.mean(inlier_rates[valid]), np.mean(inlier_nums[valid])))
                print_column_entry('Min inlier rate', '{:.1f}% / Total min: {}'.format(np.min(inlier_rates[valid]), np.min(inlier_nums[valid])))
                print_column_entry('Max inlier rate', '{:.1f}% / Total max: {}'.format(np.max(inlier_rates[valid]), np.max(inlier_nums[valid])))
                print_column_entry('Percentage results', '{:.1f} / {:.1f} / {:.1f}'.format(*percentage_stats(errors[valid], errors_rot[valid])))
                out_file.write('Mean translational error good neighbors filtered\t{:.4f} m\n'.format(np.mean(errors[valid])))
                out_file.write('Median translational error good neighbors filtered\t{:.4f} m\n'.format(np.median(errors[valid])))
                out_file.write('Max translational error good neighbors filtered\t{:.4f} m\n'.format(np.max(errors[valid])))
                out_file.write('Mean angular error good neighbors filtered\t{:.4f} °\n'.format(np.mean(errors_rot[valid])))
                out_file.write('Median angular error good neighbors filtered\t{:.4f} °\n'.format(np.median(errors_rot[valid])))
                out_file.write('Max angular error good neighbors filtered\t{:.4f} °\n'.format(np.max(errors_rot[valid])))
                if args.local_method == 'Colmap':
                    out_file.write('Average local matching rate good neighbors filtered\t{:.1f}%\n'.format(np.mean(local_matching_rate[valid])))
                out_file.write('Average inlier rate good neighbors filtered\t{:.1f}% / Total average: {}\n'.format(np.mean(inlier_rates[valid]), np.mean(inlier_nums[valid])))
                out_file.write('Min inlier rate good neighbors filtered\t{:.1f}% / Total min: {}\n'.format(np.min(inlier_rates[valid]), np.min(inlier_nums[valid])))
                out_file.write('Max inlier rate good neighbors filtered\t{:.1f}% / Total max: {}\n'.format(np.max(inlier_rates[valid]), np.max(inlier_nums[valid])))
                out_file.write('Percentage results good neighbors filtered\t{:.1f} / {:.1f} / {:.1f}\n'.format(*percentage_stats(errors[valid], errors_rot[valid])))
            else:
                print_column_entry('No images found (good neighbors)', '')
            print_seperator()
            if np.any(~valid):
                print_column_entry('Filtered by bad neighbors', '{} images'.format(errors[~valid].shape[0]))
                print_column_entry('Mean translational error', '{:.4f} m'.format(np.mean(errors[~valid])))
                print_column_entry('Median translational error', '{:.4f} m'.format(np.median(errors[~valid])))
                print_column_entry('Max translational error', '{:.4f} m'.format(np.max(errors[~valid])))
                print_column_entry('Mean angular error', '{:.4f} °'.format(np.mean(errors_rot[~valid])))
                print_column_entry('Median angular error', '{:.4f} °'.format(np.median(errors_rot[~valid])))
                print_column_entry('Max angular error', '{:.4f} °'.format(np.max(errors_rot[~valid])))
                if args.local_method == 'Colmap':
                    print_column_entry('Average local matching rate', '{:.1f}%'.format(np.mean(local_matching_rate[~valid])))
                print_column_entry('Average inlier rate', '{:.1f}% / Total average: {}'.format(np.mean(inlier_rates[~valid]), np.mean(inlier_nums[~valid])))
                print_column_entry('Min inlier rate', '{:.1f}% / Total min: {}'.format(np.min(inlier_rates[~valid]), np.min(inlier_nums[~valid])))
                print_column_entry('Max inlier rate', '{:.1f}% / Total max: {}'.format(np.max(inlier_rates[~valid]), np.max(inlier_nums[~valid])))
                print_column_entry('Percentage results', '{:.1f} / {:.1f} / {:.1f}'.format(*percentage_stats(errors[~valid], errors_rot[~valid])))
                out_file.write('Mean translational error bad neighbors filtered\t{:.4f} m\n'.format(np.mean(errors[~valid])))
                out_file.write('Median translational error bad neighbors filtered\t{:.4f} m\n'.format(np.median(errors[~valid])))
                out_file.write('Max translational error bad neighbors filtered\t{:.4f} m\n'.format(np.max(errors[~valid])))
                out_file.write('Mean angular error bad neighbors filtered\t{:.4f} °\n'.format(np.mean(errors_rot[~valid])))
                out_file.write('Median angular error bad neighbors filtered\t{:.4f} °\n'.format(np.median(errors_rot[~valid])))
                out_file.write('Max angular error bad neighbors filtered\t{:.4f} °\n'.format(np.max(errors_rot[~valid])))
                if args.local_method == 'Colmap':
                    out_file.write('Average local matching rate bad neighbors filtered\t{:.1f}%\n'.format(np.mean(local_matching_rate[~valid])))
                out_file.write('Average inlier rate bad neighbors filtered\t{:.1f}% / Total average: {}\n'.format(np.mean(inlier_rates[~valid]), np.mean(inlier_nums[~valid])))
                out_file.write('Min inlier rate bad neighbors filtered\t{:.1f}% / Total min: {}\n'.format(np.min(inlier_rates[~valid]), np.min(inlier_nums[~valid])))
                out_file.write('Max inlier rate bad neighbors filtered\t{:.1f}% / Total max: {}\n'.format(np.max(inlier_rates[~valid]), np.max(inlier_nums[~valid])))
                out_file.write('Percentage results bad neighbors filtered\t{:.1f} / {:.1f} / {:.1f}\n'.format(*percentage_stats(errors[~valid], errors_rot[~valid])))
            else:
                print_column_entry('No images found (bad neighbors)', '')

            
        print_seperator()
        valid = np.logical_and(errors < 0.5, errors_rot < 2.0)
        nbs = np.zeros_like(top_neighbor_match)
        nbs[top_neighbor_match > 0.0] = 1.0
        nbs = np.sum(nbs, axis=1)
        if np.any(valid):
            print_column_entry('Filtered by fine localized results', '{} images'.format(errors[valid].shape[0]))
            print_column_entry('Mean translational error', '{:.4f} m'.format(np.mean(errors[valid])))
            print_column_entry('Median translational error', '{:.4f} m'.format(np.median(errors[valid])))
            print_column_entry('Max translational error', '{:.4f} m'.format(np.max(errors[valid])))
            print_column_entry('Mean angular error', '{:.4f} °'.format(np.mean(errors_rot[valid])))
            print_column_entry('Median angular error', '{:.4f} °'.format(np.median(errors_rot[valid])))
            print_column_entry('Max angular error', '{:.4f} °'.format(np.max(errors_rot[valid])))
            if args.local_method == 'Colmap':
                print_column_entry('Average local matching rate', '{:.1f}%'.format(np.mean(local_matching_rate[valid])))
            print_column_entry('Average inlier rate', '{:.1f}% / Total average: {}'.format(np.mean(inlier_rates[valid]), np.mean(inlier_nums[valid])))
            print_column_entry('Min inlier rate', '{:.1f}% / Total min: {}'.format(np.min(inlier_rates[valid]), np.min(inlier_nums[valid])))
            print_column_entry('Max inlier rate', '{:.1f}% / Total max: {}'.format(np.max(inlier_rates[valid]), np.max(inlier_nums[valid])))
            print_column_entry('Average correct neighbors', '{:.1f}'.format(np.mean(nbs[valid])))
            print_column_entry('Min correct neighbors', '{:.1f}'.format(np.min(nbs[valid])))
            print_column_entry('Max correct neighbors', '{:.1f}'.format(np.max(nbs[valid])))
        else:
            print_column_entry('No images found (fine localized)', '')
            
        print_seperator()
        valid = np.logical_or(errors > 5.0, errors_rot > 10.0)
        if np.any(valid):
            print_column_entry('Filtered by wrongly localized results', '{} images'.format(np.sum(valid)))
            print_column_entry('Mean translational error', '{:.4f} m'.format(np.mean(errors[valid])))
            print_column_entry('Median translational error', '{:.4f} m'.format(np.median(errors[valid])))
            print_column_entry('Max translational error', '{:.4f} m'.format(np.max(errors[valid])))
            print_column_entry('Mean angular error', '{:.4f} °'.format(np.mean(errors_rot[valid])))
            print_column_entry('Median angular error', '{:.4f} °'.format(np.median(errors_rot[valid])))
            print_column_entry('Max angular error', '{:.4f} °'.format(np.max(errors_rot[valid])))
            if args.local_method == 'Colmap':
                print_column_entry('Average local matching rate', '{:.1f}%'.format(np.mean(local_matching_rate[valid])))
            print_column_entry('Average inlier rate', '{:.1f}% / Total average: {}'.format(np.mean(inlier_rates[valid]), np.mean(inlier_nums[valid])))
            print_column_entry('Min inlier rate', '{:.1f}% / Total min: {}'.format(np.min(inlier_rates[valid]), np.min(inlier_nums[valid])))
            print_column_entry('Max inlier rate', '{:.1f}% / Total max: {}'.format(np.max(inlier_rates[valid]), np.max(inlier_nums[valid])))
            print_column_entry('Average correct neighbors', '{:.1f}'.format(np.mean(nbs[valid])))
            print_column_entry('Min correct neighbors', '{:.1f}'.format(np.min(nbs[valid])))
            print_column_entry('Max correct neighbors', '{:.1f}'.format(np.max(nbs[valid])))
            
            print_seperator()
            print_column_entry('Identifying reasons', '{} total'.format(np.sum(valid)))
            out_file.write('Wrongly localized images: \n')
            ## bad neighbors
            bn = ~(top_neighbor_match.max(axis=1) > 0.0)
            bni = np.logical_and(bn, valid)
            bnc = np.logical_and(bn, ~valid)
            print_column_entry(' - by wrong neighbors', np.sum(bni))
            out_file.write(' - by wrong global neighbors:')
            if np.sum(bn) > 0:
                for img in np.array(query_images)[bni]:
                    out_file.write(' {},'.format(os.path.split(img)[-1].replace('.jpg', '')))
                out_file.write('\n')
            else:
                out_file.write(' None\n')
            ## few inliers
            out_file.write(' - by few inliers:')
            fi = np.logical_and(inlier_nums < 12, ~bn) #few inliers
            fii = np.logical_and(fi, valid)            #few inliers incorrect
            fic = np.logical_and(fi, ~valid)           #few inliers correct
            print_column_entry(' - by few inliers (<12)', np.sum(fii))
            assert np.sum(fii)+np.sum(fic) == np.sum(fi), 'Somethings wrong I can feel it..'
            if np.sum(fii) > 0:
                for img in np.array(query_images)[fii]:
                    out_file.write(' {},'.format(os.path.split(img)[-1].replace('.jpg', '')))
                out_file.write('\n')
            else:
                out_file.write(' None\n')
            ## low inlier rate
            out_file.write(' - by low inlier rate:')
            li = np.logical_and(inlier_rates < 10, np.logical_and(~fi, ~bn))
            lii = np.logical_and(li, valid)
            lic = np.logical_and(li, ~valid)
            assert np.sum(lii)+np.sum(lic) == np.sum(li), 'Somethings wrong I can feel it..'
            print_column_entry(' - by low inlier rate (<10%)', np.sum(lii))
            if np.sum(lii) > 0:
                for img in np.array(query_images)[lii]:
                    out_file.write(' {},'.format(os.path.split(img)[-1].replace('.jpg', '')))
                out_file.write('\n')
            else:
                out_file.write(' None\n')
            ## other
            out_file.write(' - other:')
            ot = np.logical_and(np.logical_and(~li, ~fi), ~bn)
            oti = np.logical_and(ot, valid)
            otc = np.logical_and(ot, ~valid)
            assert np.sum(oti)+np.sum(otc) == np.sum(ot), 'Somethings wrong I can feel it..'
            print_column_entry(' - other', np.sum(oti))
            if np.sum(oti) > 0:
                for img in np.array(query_images)[oti]:
                    out_file.write(' {}, '.format(''.join(list(filter(str.isdigit, os.path.split(img)[-1])))))
                out_file.write('\n')
            else:
                out_file.write(' None\n')
            print_stats(np.ones_like(errors, dtype=bool), ~valid, valid, errors, errors_rot, inlier_nums, inlier_rates, nbs, 'Not correctly localised')
            print_stats(bn, bnc, bni, errors, errors_rot, inlier_nums, inlier_rates, nbs, 'Bad neighbours')
            print_stats(fi, fic, fii, errors, errors_rot, inlier_nums, inlier_rates, nbs, 'Few inliers')
            print_stats(li, lic, lii, errors, errors_rot, inlier_nums, inlier_rates, nbs, 'Low inlier rate')
            fli = np.logical_or(fi, li)
            flii = np.logical_and(fli, valid)
            flic = np.logical_and(fli, ~valid)
            print_stats(fli, flic, flii, errors, errors_rot, inlier_nums, inlier_rates, nbs, 'Few inliers or low inlier rate')
            print_stats(ot, otc, oti, errors, errors_rot, inlier_nums, inlier_rates, nbs, 'Other cases')
            
            
        else:
            print_column_entry('No images found (wrong localized)', '')
        
        
        

        
if __name__ == '__main__':
    args = parser.parse_args()
    print_config(args)
    points3d, images, database_cursor, query_cursor, img_cluster, camera_matrices, query_images, query_image_ids, setup_time = setup(args)
    indices, image_ids = global_neighbors(args, query_images)
    out_file = open(args.out_file, 'w', buffering=1)
    image_times, errors, errors_rot, top_neighbor_match, local_matching_rate, inlier_rates, inlier_nums = local_matching(args, points3d, images, database_cursor, query_cursor, img_cluster, camera_matrices, query_images, query_image_ids, indices, image_ids, out_file)   
    stats(args, setup_time, image_times, errors, errors_rot, out_file, top_neighbor_match, local_matching_rate, inlier_rates, inlier_nums)
    out_file.close()
    
    

