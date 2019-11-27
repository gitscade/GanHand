import torch
from collections import OrderedDict
from torch.autograd import Variable
import utils.util as util
import utils.plots as plot_utils
from .models import BaseModel
from networks.networks import NetworksFactory
import os
import numpy as np

from manopth.manolayer import ManoLayer
from manopth import demo

import trimesh
import socket

import point_cloud_utils as pcu

from utils import forward_kinematics_3dof as fk
#from utils import forward_kinematics_doublepenalisation as fk
from utils import contactutils


class Model(BaseModel):
    def __init__(self, opt):
        super(Model, self).__init__(opt)
        self._name = 'Model_rgb_to_hand_cluster'

        self._touching_hand_vertices = [ 73,  96,  98,  99, 309, 317, 318, 319, 320, 322, 323, 324, 325,
       326, 327, 328, 329, 332, 333, 337, 338, 339, 343, 347, 348, 349,
       350, 351, 352, 353, 354, 355, 429, 433, 434, 435, 436, 437, 438,
       439, 442, 443, 444, 455, 461, 462, 463, 465, 466, 467, 547, 548,
       549, 550, 553, 566, 573, 578, 657, 661, 662, 664, 665, 666, 667,
       670, 671, 672, 677, 678, 683, 686, 687, 688, 689, 690, 691, 692,
       693, 694, 695, 736, 737, 738, 739, 740, 741, 743, 753, 754, 755,
       756, 757, 759, 760, 761, 762, 763, 764, 766, 767, 768, 772, 774,
       775, 777]

        # create networks
        self._init_create_networks()

        # init train variables
        if self._is_train:
            self._init_train_vars()

        # load networks and optimizers
        if not self._is_train or self._opt.load_epoch > 0:
            self.load()

        # init
        self._init_losses()
        self.clusters = np.load('clustersk' + str(self._opt.k) + 'norot.npy')
        self.clusters_tensor = torch.FloatTensor(np.load('clustersk' + str(self._opt.k) + 'norot.npy')).cuda()

        self._gradient_accumulation_every = 5
        self._gradient_accumulation_current_step = 0

        self._use_approach = False

    def _init_create_networks(self):
        # generator network
        self._G = self._create_generator()
        #self._G.init_weights()
        if len(self._gpu_ids) > 1:
            self._G = torch.nn.DataParallel(self._G, device_ids=self._gpu_ids)
        if torch.cuda.is_available():
            self._G.cuda()

        self._FC = self._create_fcnet()
        self._FC.init_weights()
        if len(self._gpu_ids) > 1:
            self._FC = torch.nn.DataParallel(self._FC, device_ids=self._gpu_ids)
        if torch.cuda.is_available():
            self._FC.cuda()

        # Initialize MANO layer
        mano_layer_right = ManoLayer(
            mano_root='/home/ecorona/hand_grasppoint_gan/manopth/mano/models/', side='right', use_pca=True, ncomps=45, flat_hand_mean=True)
        if torch.cuda.is_available():
            mano_layer_right = mano_layer_right.cuda()
        self._MANO = mano_layer_right

        # Discriminator network
        self._D = self._create_discriminator()
        self._D.init_weights()
        if torch.cuda.is_available():
            self._D.cuda()


        # LOAD ATLASNET PRETRAINED NETS ON THIS DATASET!
        print("UPDATE ATLASNET PRETRAINED MODEL TO SOME REGULARISED NET!")
        print("UPDATE ATLASNET PRETRAINED MODEL TO SOME REGULARISED NET!")
        print("UPDATE ATLASNET PRETRAINED MODEL TO SOME REGULARISED NET!")

        from networks import resnet_obman
        self._encoder_atlasnet = resnet_obman.resnet18(pretrained=True)
        self._encoder_atlasnet.load_state_dict(torch.load('atlasnet_rgb_1410_notrans_0_0_copynew64/experiment_1/net_epoch_50_id_encoder.pth'))
        self._encoder_atlasnet = self._encoder_atlasnet.cuda()

        self._atlasnet = self._create_atlasnet()
        self._atlasnet.load_state_dict(torch.load('atlasnet_rgb_1410_notrans_0_0_copynew64/experiment_1/net_epoch_50_id_G.pth'))
        self._atlasnet = self._atlasnet.cuda()

        self._encoder_atlasnet = self._encoder_atlasnet.eval()
        self._atlasnet = self._atlasnet.eval()

    def _create_generator(self):
        net = NetworksFactory.get_by_name('depth_to_hand_joints_and_imgrep', input_chann=3, output_dim=self._opt.k) # 3-rot, 45-PCA, 3-translation

        # Loading net trained first
        pc_name = socket.gethostname()
        if pc_name == 'tro':
            net.load_state_dict(torch.load('/DATA/ecorona/nips19/classifier_k25_norot_wimg_double_weighted/experiment_1/net_epoch_30_id_G.pth'))
        elif pc_name == 'visen3':
            net.load_state_dict(torch.load('/DataDisks/home/ecorona/grasp_GAN/classifier_k25_norot_wimg_double_weighted/experiment_1/net_epoch_30_id_G.pth'))
        else:
            net.load_state_dict(torch.load('/home/ecorona/GHANds/grasp_GAN/pretrained_grasp_classifier_rgb256/experiment_1/net_epoch_3_id_G.pth'))
        print("Using pretrained resnet50 weighted")
        return net


    def _create_fcnet(self):
        return NetworksFactory.get_by_name('refine_from_RrTtI_nonormals', output_dim=51) # 3-rot, 45-PCA, 3-translation

    def _create_discriminator(self):
        return NetworksFactory.get_by_name('discriminator_smpl+C_deeper', input_size=51)

    def _create_atlasnet(self):
        return NetworksFactory.get_by_name('atlasnet_as_in_obman')

    def _init_train_vars(self):
        self._current_lr_G = self._opt.lr_G
        self._current_lr_D = self._opt.lr_D

        # initialize optimizers
        self._optimizer_G = torch.optim.Adam(self._G.parameters(), lr=self._current_lr_G/10,
                                             betas=[self._opt.G_adam_b1, self._opt.G_adam_b2])
        self._optimizer_D = torch.optim.Adam(self._D.parameters(), lr=self._current_lr_D,
                                             betas=[self._opt.D_adam_b1, self._opt.D_adam_b2])
        self._optimizer_FC = torch.optim.Adam(self._FC.parameters(), lr=self._current_lr_G)

    def set_epoch(self, epoch):
        self.iepoch = epoch

    def _init_losses(self):
        # init losses G
        self._loss_g_CE = Variable(self._Tensor([0]))
        self._acc_g = Variable(self._Tensor([0]))
        self._loss_g_fake = Variable(self._Tensor([0]))
        self._loss_g_contactloss = Variable(self._Tensor([0]))
        self._loss_g_interpenetration = Variable(self._Tensor([0]))
        self._loss_g_fk = Variable(self._Tensor([0]))
        self._loss_g_angles = Variable(self._Tensor([0]))
        self._loss_g_angles_2ndjoint = Variable(self._Tensor([0]))
        self._loss_g_angles_3rdjoint = Variable(self._Tensor([0]))

        self._loss_d_real = Variable(self._Tensor([0]))
        self._loss_d_fake = Variable(self._Tensor([0]))
        self._loss_d_fakeminusreal = Variable(self._Tensor([0]))
        self._loss_d_gp = Variable(self._Tensor([0]))
        self._criterion_CE = torch.nn.CrossEntropyLoss().cuda()

    def set_input(self, input):
        self._input_rgb_img = input['rgb_img'].float().permute(0, 3, 1, 2).contiguous()

        self._input_noise_img = input['noise_img'].float()
        self._input_coords_3d = input['coords_3d'].float()
        self._input_label = input['label']
        self._input_cluster = np.array(input['cluster'])
        self._input_obj_verts = input['3d_points_object']
        self._input_obj_faces = input['3d_faces_object']
        self._input_coords_3d = input['coords_3d'].float()
        self._input_pca_poses = input['pca_poses'].float()
        self._input_hand_exact_pose = input['verts_3d']
        self._input_object_translation = input['object_translation'].float()
        self._input_rot_poses = input['mano_rotation'].float()
        self._input_trans_poses = input['mano_translation'].float()

        if torch.cuda.is_available():
            self._input_rgb_img = self._input_rgb_img.cuda()
            self._input_noise_img = self._input_noise_img.cuda()
            self._input_coords_3d = self._input_coords_3d.cuda()
            self._input_label = self._input_label.cuda()
            self._input_coords_3d = self._input_coords_3d.cuda()
            self._input_pca_poses = self._input_pca_poses.cuda()
            self._input_object_translation = self._input_object_translation.cuda()
            self._input_rot_poses = self._input_rot_poses.cuda()
            self._input_trans_poses = self._input_trans_poses.cuda()

        self._B = self._input_rgb_img.size(0)
        return 

    def set_train(self):
        self._G.train()
        self._FC.train()
        self._D.train()
        self._is_train = True

    def set_eval(self):
        self._G.eval()
        self._FC.eval()
        self._D.eval()
        self._is_train = False

    # get image paths
    def get_image_paths(self):
        return OrderedDict([]) #('real_img', self._input_real_img_path)])

    def forward(self, keep_data_for_visuals=False):
        if not self._is_train:
            # Predict object mesh:
            rgb_img = self._input_rgb_img

            img_features, _ = self._encoder_atlasnet(rgb_img)
            prediction = self._atlasnet.forward_inference(img_features.detach())#['objpoints3d']
            #prediction['objpoints3d'] = prediction['objpoints3d'].detach()
            prediction['objscale'] = prediction['objscale'].detach()

            self._predicted_obj_verts = prediction['objpoints3d'].detach() #+ self._input_object_translation.unsqueeze(1)
            self._predicted_obj_faces = prediction['objfaces']

            # convert tensor to variables
            prediction, img_representations = self._G.forward(rgb_img)
            self._loss_g_CE = self._criterion_CE(prediction, self._input_label)/100
            self._acc_g = (prediction.cpu().data.numpy().argmax(1) == self._input_label.cpu().data.numpy()).mean()

            # TODO: weight clusters correctly in their manifold!
            hand_representations = self.hand_representation_from_prediction(prediction)

            if self._use_approach:
                random_rots = torch.FloatTensor(self._approach_orientation).cuda().unsqueeze(0).repeat(self._B, 1)
            else:
                #random_rots = self._input_hand_gt_rep[:, :3] + torch.normal(mean=torch.zeros(self._B, 3)).cuda()/5
                random_rots = torch.FloatTensor(np.random.normal(0, 1, (self._B, 3)))
                random_rots = random_rots.cuda()

            #HR, R, T = self._FC.forward(img_representations, hand_representations, random_rots, self._input_object_translation)
            HR, R, T = self._FC.forward(img_representations, hand_representations, random_rots, torch.zeros(self._B, 3).cuda())
            HR = HR.detach()
            T = T.detach()
            R = R.detach()
            points_3d, _ = self._MANO(torch.cat((R, HR), -1), th_trans = T)

            # GT IS IN meters, WHILE MANO IS IN mm
            points_3d = points_3d / 1000

            # Finger vertices loss:
            distance_touching_vertices_fake = self._get_touching_distances(points_3d, self._predicted_obj_verts)
            self._loss_g_contactloss = distance_touching_vertices_fake.mean()*self._opt.lambda_G_contactloss

            fake_input_D = torch.cat((R,
                                HR,
                                T), 1)
            real_input_D = torch.cat((self._input_rot_poses,
                                    self._input_pca_poses,
                                    self._input_trans_poses - self._input_object_translation), 1).detach()
            d_fake_prob = self._D(fake_input_D)
            d_real_prob = self._D(real_input_D)
            self._loss_g_fake = self._compute_loss_D(d_fake_prob, True)*self._opt.lambda_D_prob
            self._loss_d_fake = self._compute_loss_D(d_fake_prob, False)*self._opt.lambda_D_prob
            self._loss_d_real = self._compute_loss_D(d_real_prob, True)*self._opt.lambda_D_prob

            handfullpose = fk.get_fullhandpose(HR)
            loss_fk = []
            loss_angles = []
            loss_angles_2ndjoint = []
            loss_angles_3rdjoint = []
            handfullposes = []
            all_touching_indexs = []
            for i in range(self._B):
                handfullpose_list, touching_indexs, loss_distance, loss_angle, loss_angle_2ndjoint, loss_angle_3rdjoint = fk.optimize_hand(handfullpose[i].unsqueeze(0), R[i].unsqueeze(0), T[i].unsqueeze(0), self._predicted_obj_verts[i].cpu().data.numpy(), step=5) # Just doing every 5 points to not collapse gpu mem
                print(touching_indexs)
                all_touching_indexs.append(touching_indexs)
                loss_fk.append(loss_distance)
                loss_angles.append(loss_angle)
                loss_angles_2ndjoint.append(loss_angle_2ndjoint)
                loss_angles_3rdjoint.append(loss_angle_3rdjoint)

                handfullposes.append(handfullpose_list)

            if self._opt.lambda_G_angles > 0 or self._opt.lambda_G_fk > 0:
                self._loss_g_fk = torch.stack(loss_fk).mean()*self._opt.lambda_G_fk
                self._loss_g_angles = torch.stack(loss_angles).mean()*self._opt.lambda_G_angles
                self._loss_g_angles_2ndjoint = torch.stack(loss_angles_2ndjoint).mean()*self._opt.lambda_G_angles
                self._loss_g_angles_3rdjoint = torch.stack(loss_angles_3rdjoint).mean()*self._opt.lambda_G_angles

                handfullpose = torch.cat(handfullposes)

            points_3d, joints_3d = fk.get_hand(handfullpose, R, T)

            interpenetration = torch.FloatTensor([0]).cuda()
            # INTERSECTION LOSS ON OPTIMIZED HAND!
            for i in range(self._B):
                obj_triangles = self._predicted_obj_verts[i][self._predicted_obj_faces]

                exterior = contactutils.batch_mesh_contains_points(
                    points_3d[i].detach().unsqueeze(0), obj_triangles.unsqueeze(0)
                )
                penetr_mask = ~exterior

                print(penetr_mask.sum())
                if penetr_mask.sum()==0:
                    continue

                dists = util.batch_pairwise_dist(points_3d[i, penetr_mask[0]].unsqueeze(0), self._predicted_obj_verts[i].unsqueeze(0))
                #mins12, min12idxs = torch.min(dists, 1)
                mins21, _ = torch.min(dists, 2)

                interpenetration = interpenetration + mins21.mean()

            print("")

            self._loss_g_interpenetration = interpenetration/self._B * self._opt.lambda_G_intersections



            if keep_data_for_visuals:
                self.predictions_label = prediction
                self._touching_indexs = all_touching_indexs
                self._refined_handpose = points_3d[0].cpu().data.numpy()
                self._refined_HR = HR
                self._refined_R = R
                self._refined_T = T

            return prediction

    def _get_touching_distances(self, hand_points, object_points):
        relevant_vertices = hand_points[:, self._touching_hand_vertices]

        distances = []
        n1 = len(self._touching_hand_vertices)
        # TODO: HAVE TO DO IT IN A LOOP SINCE OBJECTS ALL HAVE DIFFERENT AMOUNT OF VERTICES
        #for i in range(self._B):
        #    n2 = len(object_points[i])

        #    matrix1 = relevant_vertices[i].unsqueeze(0).repeat(n2, 1, 1)
        #    matrix2 = torch.FloatTensor(object_points[i]).cuda().unsqueeze(1).repeat(1, n1, 1)
        #    dists = torch.sqrt(((matrix1-matrix2)**2).sum(-1))
        #    distances.append(dists.min(0)[0])
        relevant_vertices = relevant_vertices.unsqueeze(2)
        object_points = object_points.unsqueeze(1)
        dists = torch.sqrt(((relevant_vertices - object_points)**2).sum(-1))
        dists = dists.min(2)[0]

        return dists
        #return torch.stack(distances)

    def _get_distances_single_example(self, relevant_vertices, object_points):
        distances = []
        n1 = len(relevant_vertices) 
        # TODO: HAVE TO DO IT IN A LOOP SINCE OBJECTS ALL HAVE DIFFERENT AMOUNT OF VERTICES
        n2 = len(object_points)

        matrix1 = relevant_vertices.unsqueeze(0).repeat(n2, 1, 1)
        if torch.cuda.is_available():
            matrix2 = torch.FloatTensor(object_points).cuda().unsqueeze(1).repeat(1, n1, 1)
        else:
            matrix2 = torch.FloatTensor(object_points).unsqueeze(1).repeat(1, n1, 1)
        dists = torch.sqrt(((matrix1-matrix2)**2).sum(-1))
        return dists.min(0)[0]

    def optimize_parameters(self, train_generator=True, keep_data_for_visuals=False):
        if self._is_train:
            # convert tensor to variables
            self._B = self._input_rgb_img.size(0)

            # train D
            fake_input_D, real_input_D, loss_D = self._forward_D()
            self._optimizer_D.zero_grad()
            loss_D.backward(retain_graph=True)
            self._optimizer_D.step()

            loss_D_gp = self._gradinet_penalty_D(fake_input_D, real_input_D)
            self._optimizer_D.zero_grad()
            loss_D_gp.backward()
            self._optimizer_D.step()

            # train G
            if train_generator:
                loss_G = self._forward_G(keep_data_for_visuals)
                loss_G.backward()
                self._gradient_accumulation_current_step += 1 

                if self._gradient_accumulation_current_step%self._gradient_accumulation_every==0:
                    self._optimizer_G.step()
                    self._optimizer_FC.step()

                    self._optimizer_G.zero_grad()
                    self._optimizer_FC.zero_grad()
                    self._gradient_accumulation_current_step = 0
                    print("Generator step done!!")


    def _forward_G(self, keep_data_for_visuals):
        rgb_img = self._input_rgb_img

        img_features, _ = self._encoder_atlasnet(rgb_img)
        prediction = self._atlasnet.forward_inference(img_features)#['objpoints3d']
        #prediction['objpoints3d'] = prediction['objpoints3d'].detach()
        prediction['objscale'] = prediction['objscale'].detach()

        self._predicted_obj_verts = prediction['objpoints3d'].detach() #+ self._input_object_translation.unsqueeze(1)
        self._predicted_obj_faces = prediction['objfaces']

        prediction, img_representations = self._G.forward(rgb_img)
        self._loss_g_CE = self._criterion_CE(prediction, self._input_label)/100
        self._acc_g = (prediction.cpu().data.numpy().argmax(1) == self._input_label.cpu().data.numpy()).mean()

        # TODO: weight clusters correctly in their manifold!
        hand_representations = self.hand_representation_from_prediction(prediction)
        random_rots = torch.FloatTensor(np.random.normal(0, 1, (self._B, 3))).cuda()
        #random_rots = torch.FloatTensor([[-0.5360,  0.8917, -0.6528]]).cuda()
        #HR, R, T = self._FC.forward(img_representations, hand_representations, random_rots, self._input_object_translation)
        HR, R, T = self._FC.forward(img_representations, hand_representations, random_rots, torch.zeros(self._B, 3).cuda())
        points_3d, _ = self._MANO(torch.cat((R, HR), -1), th_trans = T)

        # GT IS IN meters, WHILE MANO IS IN mm
        points_3d = points_3d / 1000


        interpenetration = torch.FloatTensor([0]).cuda()
        # INTERSECTION LOSS ON OPTIMIZED HAND!
        for i in range(self._B):
            obj_triangles = self._predicted_obj_verts[i][self._predicted_obj_faces]

            exterior = contactutils.batch_mesh_contains_points(
                points_3d[i].detach().unsqueeze(0), obj_triangles.unsqueeze(0)
            )
            penetr_mask = ~exterior

            if penetr_mask.sum()==0:
                continue

            dists = util.batch_pairwise_dist(points_3d[i, penetr_mask[0]].unsqueeze(0), self._predicted_obj_verts[i].unsqueeze(0))
            #mins12, min12idxs = torch.min(dists, 1)
            mins21, _ = torch.min(dists, 2)

            interpenetration = interpenetration + mins21.mean()

        self._loss_g_interpenetration = interpenetration/self._B * self._opt.lambda_G_intersections

        # NOTE: NEW RESAMPLING OF OBJECT VERTICES TO MAKE DISTANCES TO FINGERS REAL:

        distance_touching_vertices_fake = self._get_touching_distances(points_3d, self._predicted_obj_verts)
        self._loss_g_contactloss = distance_touching_vertices_fake.mean()*self._opt.lambda_G_contactloss

        fake_input_D = torch.cat((R,
                            HR,
                            T), 1)
        d_fake_prob = self._D(fake_input_D)
        self._loss_g_fake = self._compute_loss_D(d_fake_prob, True)*self._opt.lambda_D_prob

        # LOSS FORWARD KINEMATICS:
        if self._opt.lambda_G_angles > 0 or self._opt.lambda_G_fk > 0:
            handfullpose = fk.get_fullhandpose(HR)
            loss_fk = []
            loss_angles = []
            loss_angles_2ndjoint = []
            loss_angles_3rdjoint = []
            for i in range(self._B):
                handfullpose_list, touching_indexs, loss_distance, loss_angle, loss_angle_2ndjoint, loss_angle_3rdjoint = fk.optimize_hand(handfullpose[i].unsqueeze(0), R[i].unsqueeze(0), T[i].unsqueeze(0), self._predicted_obj_verts[i].cpu().data.numpy()) # Just doing every 5 points to not collapse gpu mem
                loss_fk.append(loss_distance)
                loss_angles.append(loss_angle)
                loss_angles_2ndjoint.append(loss_angle_2ndjoint)
                loss_angles_3rdjoint.append(loss_angle_3rdjoint)

            self._loss_g_fk = torch.stack(loss_fk).mean()*self._opt.lambda_G_fk
            self._loss_g_angles = torch.stack(loss_angles).mean()*self._opt.lambda_G_angles
            self._loss_g_angles_2ndjoint = torch.stack(loss_angles_2ndjoint).mean()*self._opt.lambda_G_angles
            self._loss_g_angles_3rdjoint = torch.stack(loss_angles_3rdjoint).mean()*self._opt.lambda_G_angles

            points_3d, joints_3d = fk.get_hand(handfullpose, R, T)

        if keep_data_for_visuals:
            self.predictions_label = prediction
            self._refined_handpose = points_3d[0].cpu().data.numpy()

        # combine losses
        return self._loss_g_CE + self._loss_g_fake + self._loss_g_interpenetration + self._loss_g_fk + self._loss_g_angles + self._loss_g_angles_2ndjoint + self._loss_g_angles_3rdjoint + self._loss_g_contactloss

    def get_closest(matrix1, matrix2):
     n1 = len(matrix1)
     n2 = len(matrix2)
     matrix1 = matrix1[np.newaxis].repeat(n2, 0)
     matrix2 = matrix2[:, np.newaxis].repeat(n1, 1)
     dists = ((matrix1-matrix2)**2).sum(-1)
     closest1s = dists.argmin(0)
     closest2s = dists.argmin(1)
     return closest1s, closest2s

    def _forward_D(self):
        rgb_img = self._input_rgb_img
        prediction, img_representations = self._G.forward(rgb_img)
        hand_representations = self.hand_representation_from_prediction(prediction)

        # TODO: weight clusters correctly in their manifold!
        random_rots = torch.FloatTensor(np.random.normal(0, 1, (self._B, 3))).cuda()
        #HR, R, T = self._FC.forward(img_representations, hand_representations, random_rots, self._input_object_translation)
        HR, R, T = self._FC.forward(img_representations, hand_representations, random_rots, torch.zeros(self._B, 3).cuda())
        HR = HR.detach()
        R = R.detach()
        T = T.detach()

        # NOTE COULD JUST USE GT HAND VERTEXS TO OBTAIN TOUCHING DISTANCES
        fake_input_D = torch.cat((R,
                                HR,
                                T), 1).detach()
        real_input_D = torch.cat((self._input_rot_poses,
                                self._input_pca_poses,
                                self._input_trans_poses - self._input_object_translation), 1).detach()
        d_fake_prob = self._D(fake_input_D)
        d_real_prob = self._D(real_input_D)

        self._loss_d_real = self._compute_loss_D(d_real_prob, True)*self._opt.lambda_D_prob
        self._loss_d_fake = self._compute_loss_D(d_fake_prob, False)*self._opt.lambda_D_prob

        return fake_input_D, real_input_D, self._loss_d_real + self._loss_d_fake

    def hand_representation_from_prediction(self, prediction):
        prediction_probs = torch.nn.Softmax(-1)(prediction)
        hand_representations = self.clusters_tensor.unsqueeze(0).repeat(self._B, 1, 1)*prediction_probs.unsqueeze(-1).repeat(1, 1, 45)
        hand_representations = hand_representations.sum(1)
        return hand_representations

    def _gradinet_penalty_D(self, fake_input_D, real_input_D):
        # interpolate sample
        alpha = torch.rand(self._B, fake_input_D.shape[1]).cuda()
        alpha.requires_grad = True
        interpolated = alpha * real_input_D + (1 - alpha) * fake_input_D
        interpolated_prob = self._D(interpolated)

        # compute gradients
        grad = torch.autograd.grad(outputs=interpolated_prob,
                                   inputs=interpolated,
                                   grad_outputs=torch.ones(interpolated_prob.size()).cuda(),
                                   retain_graph=True,
                                   create_graph=True,
                                   only_inputs=True)[0]

        # penalize gradients
        grad = grad.view(grad.size(0), -1)
        grad_l2norm = torch.sqrt(torch.sum(grad ** 2, dim=1))
        self._loss_d_gp = torch.mean((grad_l2norm - 1) ** 2) * self._opt.lambda_D_gp

        return self._loss_d_gp

    def _compute_loss_D(self, estim, is_real):
        return -torch.mean(estim) if is_real else torch.mean(estim)


    def get_current_errors(self):
        loss_dict = OrderedDict([('g_CE', self._loss_g_CE.cpu().data.numpy()),
                                 ('g_acc', self._acc_g),
                                 ('g_fake', self._loss_g_fake.cpu().data.numpy()),
                                 ('g_contactloss', self._loss_g_contactloss.cpu().data.numpy()),
                                 ('g_intersections', self._loss_g_interpenetration.cpu().data.numpy()),
                                 ('g_fk', self._loss_g_fk.cpu().data.numpy()),
                                 ('g_angles', self._loss_g_angles.cpu().data.numpy()),
                                 ('g_angles_2ndjoint', self._loss_g_angles_2ndjoint.cpu().data.numpy()),
                                 ('g_angles_3rdjoint', self._loss_g_angles_3rdjoint.cpu().data.numpy()),
                                 ('d_real',self._loss_d_real.cpu().data.numpy()),
                                 ('d_fake',self._loss_d_fake.cpu().data.numpy()),
                                 ('d_fakeminusreal',self._loss_d_fake.cpu().data.numpy() - self._loss_d_real.cpu().data.numpy()),
                                 ('d_gp',self._loss_d_gp.cpu().data.numpy()),
                                 ])

        return loss_dict

    def get_current_scalars(self):
        return OrderedDict([('lr_G', self._current_lr_G)])

    def get_current_visuals(self):
        # visuals return dictionary
        visuals = OrderedDict()

        try:
            #hand_predicted, hand_joints = self._MANO(torch.FloatTensor([[0, 0, 0] + self.clusters[self.predictions_label[0].argmax()].tolist()]).cuda())
            #hand_gt, hand_joints = self._MANO(torch.FloatTensor([[0, 0, 0] + self._input_cluster[0, :].tolist()]).cuda())
            hand_faces = self._MANO.th_faces.cpu().data.numpy()
            #hand_clusters = hand_predicted[0].cpu().data.numpy()/1000

            #visuals['4_everything'] = plot_utils.plot_everything(self._input_rgb_img[0].cpu().data.numpy(), self._predicted_obj_verts[0].cpu().data.numpy(), self._predicted_obj_faces, self._input_hand_exact_pose[0], hand_clusters, self._refined_handpose, hand_faces)
            visuals['0_prediction'] = plot_utils.plot_hand_w_object(self._predicted_obj_verts[0].cpu().data.numpy(), self._predicted_obj_faces, self._refined_handpose, hand_faces)
        except:
            pass

        return visuals

    def save(self, label):
        # save networks
        self._save_network(self._G, 'G', label)
        self._save_network(self._FC, 'FC', label)
        self._save_network(self._D, 'D', label)

        # save optimizers
        self._save_optimizer(self._optimizer_G, 'G', label)
        self._save_optimizer(self._optimizer_FC, 'FC', label)
        self._save_optimizer(self._optimizer_D, 'D', label)

    def load(self):
        load_epoch = self._opt.load_epoch

        # load G
        self._load_network(self._G, 'G', load_epoch)
        self._load_network(self._FC, 'FC', load_epoch)
        self._load_network(self._D, 'D', load_epoch)

        print(self._is_train)
        if self._is_train:
            # load optimizers
            self._load_optimizer(self._optimizer_G, 'G', load_epoch)
            self._load_optimizer(self._optimizer_FC, 'FC', load_epoch)
            self._load_optimizer(self._optimizer_D, 'D', load_epoch)

    def update_learning_rate(self):
        # updated learning rate G
        lr_decay_G = self._opt.lr_G / self._opt.nepochs_decay
        self._current_lr_G -= lr_decay_G
        for param_group in self._optimizer_G.param_groups:
            param_group['lr'] = self._current_lr_G/10
        print('update G learning rate: %f -> %f' %  (self._current_lr_G/10 + lr_decay_G/10, self._current_lr_G/10))
        for param_group in self._optimizer_FC.param_groups:
            param_group['lr'] = self._current_lr_G
        print('update FC learning rate: %f -> %f' %  (self._current_lr_G + lr_decay_G, self._current_lr_G))
        for param_group in self._optimizer_D.param_groups:
            param_group['lr'] = self._current_lr_G
        print('update D learning rate: %f -> %f' %  (self._current_lr_G + lr_decay_G, self._current_lr_G))
