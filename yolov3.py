# -*- coding: utf-8 -*-
"""yolov3.ipynb

Automatically generated by Colaboratory.

Original file is located at
    https://colab.research.google.com/drive/169GDnom9d0EqA7mp1Fsp0HPSi_kR81yt
"""

#-----------------------Library import------------------------------#
from __future__ import division
import random
import torch 
import torch.nn as nn
import torch.nn.functional as F 
from torch.autograd import Variable
import numpy as np
import argparse
import os 
import cv2 
import time
import numpy as np
import cv2 
import argparse
import os 
import os.path as osp
import pickle as pkl
import pandas as pd
import random
import cv2

####---------------------Getting Files from the web----------------------####
!wget https://raw.githubusercontent.com/pjreddie/darknet/master/cfg/yolov3.cfg
num_classes = 80    #For COCO
!wget https://raw.githubusercontent.com/ayooshkathuria/YOLO_v3_tutorial_from_scratch/master/data/coco.names
!wget https://pjreddie.com/media/files/yolov3.weights

#####--------Parsing the CFG file-------------##
def parse_cfg(cfgfile):
  file=open(cfgfile,'r')
  lines=file.read().split('\n')
  lines=[x for x in lines if len(x)>0]
  lines=[x for x in lines if x[0]!='#']
  lines=[x.rstrip().lstrip() for x in lines]
  block = {}
  blocks = []

  for line in lines:
      if line[0] == "[":               # This marks the start of a new block
          if len(block) != 0:          # If block is not empty, implies it is storing values of previous block.
              blocks.append(block)     # add it the blocks list
              block = {}               # re-init the block
          block["type"] = line[1:-1].rstrip()     
      else:
          key,value = line.split("=") 
          block[key.rstrip()] = value.lstrip()
  blocks.append(block)

  return blocks

####---------------------------Subclassing Network modules from Pytorch-----------------------------#####
class MaxPoolStride1(nn.Module):
    def __init__(self, kernel_size):
        super(MaxPoolStride1, self).__init__()
        self.kernel_size = kernel_size
        self.pad = kernel_size - 1
    
    def forward(self, x):
        padded_x = F.pad(x, (0,self.pad,0,self.pad), mode="replicate")
        pooled_x = nn.MaxPool2d(self.kernel_size, self.pad)(padded_x)
        return pooled_x
    

class EmptyLayer(nn.Module):
    def __init__(self):
        super(EmptyLayer, self).__init__()
        

class DetectionLayer(nn.Module):
    def __init__(self, anchors):
        super(DetectionLayer, self).__init__()
        self.anchors = anchors
    
    def forward(self, x, inp_dim, num_classes, confidence):
        x = x.data
        global CUDA
        prediction = x
        prediction = predict_transform(prediction, inp_dim, self.anchors, num_classes, confidence,CUDA)
        return prediction
        
class Upsample(nn.Module):
    def __init__(self, stride=2):
        super(Upsample, self).__init__()
        self.stride = stride
        
    def forward(self, x):
        stride = self.stride
        assert(x.data.dim() == 4)
        B = x.data.size(0)
        C = x.data.size(1)
        H = x.data.size(2)
        W = x.data.size(3)
        ws = stride
        hs = stride
        x = x.view(B, C, H, 1, W, 1).expand(B, C, H, stride, W, stride).contiguous().view(B, C, H*stride, W*stride)
        return x     
        
class ReOrgLayer(nn.Module):
    def __init__(self, stride = 2):
        super(ReOrgLayer, self).__init__()
        self.stride= stride
        
    def forward(self,x):
        assert(x.data.dim() == 4)
        B,C,H,W = x.data.shape
        hs = self.stride
        ws = self.stride
        assert(H % hs == 0),  "The stride " + str(self.stride) + " is not a proper divisor of height " + str(H)
        assert(W % ws == 0),  "The stride " + str(self.stride) + " is not a proper divisor of height " + str(W)
        x = x.view(B,C, H // hs, hs, W // ws, ws).transpose(-2,-3).contiguous()
        x = x.view(B,C, H // hs * W // ws, hs, ws)
        x = x.view(B,C, H // hs * W // ws, hs*ws).transpose(-1,-2).contiguous()
        x = x.view(B, C, ws*hs, H // ws, W // ws).transpose(1,2).contiguous()
        x = x.view(B, C*ws*hs, H // ws, W // ws)
        return x

###############----------------------------Function to create module---------------------######
def create_modules(blocks):
    net_info = blocks[0]     #Captures the information about the input and pre-processing    
    module_list = nn.ModuleList()
    prev_filters = 3
    output_filters = []
    for index, x in enumerate(blocks[1:]):
        module = nn.Sequential()
        if (x["type"] == "convolutional"):
            #Get the info about the layer
            activation = x["activation"]
            try:
                batch_normalize = int(x["batch_normalize"])
                bias = False
            except:
                batch_normalize = 0
                bias = True

            filters= int(x["filters"])
            padding = int(x["pad"])
            kernel_size = int(x["size"])
            stride = int(x["stride"])

            if padding:
                pad = (kernel_size - 1) // 2
            else:
                pad = 0

            #Add the convolutional layer
            conv = nn.Conv2d(prev_filters, filters, kernel_size, stride, pad, bias = bias)
            module.add_module("conv_{0}".format(index), conv)

            #Add the Batch Norm Layer
            if batch_normalize:
                bn = nn.BatchNorm2d(filters)
                module.add_module("batch_norm_{0}".format(index), bn)

            #Check the activation. 
            #It is either Linear or a Leaky ReLU for YOLO
            if activation == "leaky":
                activn = nn.LeakyReLU(0.1, inplace = True)
                module.add_module("leaky_{0}".format(index), activn)

        #If it's an upsampling layer
        #We use Bilinear2dUpsampling
        elif (x["type"] == "upsample"):
            stride = int(x["stride"])
            upsample = nn.Upsample(scale_factor = 2, mode = "bilinear")
            module.add_module("upsample_{}".format(index), upsample)
        elif (x["type"] == "route"):
            x["layers"] = x["layers"].split(',')
            #Start  of a route
            start = int(x["layers"][0])
            #end, if there exists one.
            try:
                end = int(x["layers"][1])
            except:
                end = 0
            #Positive anotation
            if start > 0: 
                start = start - index
            if end > 0:
                end = end - index
            route = EmptyLayer()
            module.add_module("route_{0}".format(index), route)
            
            route = EmptyLayer()
            
            
            
            if end < 0:
                filters = output_filters[index + start] + output_filters[index + end]
            else:
                filters= output_filters[index + start]

        #shortcut corresponds to skip connection
        elif x["type"] == "shortcut":
            shortcut = EmptyLayer()
            module.add_module("shortcut_{}".format(index), shortcut)
        elif x["type"] == "maxpool":
            stride = int(x["stride"])
            size = int(x["size"])
            if stride != 1:
                maxpool = nn.MaxPool2d(size, stride)
            else:
                maxpool = MaxPoolStride1(size)
            
            module.add_module("maxpool_{}".format(index), maxpool)
        elif x["type"] == "yolo":
            mask = x["mask"].split(",")
            mask = [int(x) for x in mask]

            anchors = x["anchors"].split(",")
            anchors = [int(a) for a in anchors]
            anchors = [(anchors[i], anchors[i+1]) for i in range(0, len(anchors),2)]
            anchors = [anchors[i] for i in mask]

            detection = DetectionLayer(anchors)
            module.add_module("Detection_{}".format(index), detection)
        else:
            print("Something I dunno")
            assert False
        
        module_list.append(module)
        prev_filters = filters
        output_filters.append(filters)
        
    return (net_info, module_list)

#########--------------------------Define Darknet Class to take in the cfg file,create object that will have the weights and names ready to predict---------------##########
class Darknet(nn.Module):
  gllayer = []
  def __init__(self, cfgfile):
        super(Darknet, self).__init__()
        self.blocks = parse_cfg(cfgfile)
        self.net_info, self.module_list = create_modules(self.blocks)
        self.header = torch.IntTensor([0,0,0,0])
        self.seen = 0

        
        
  def get_blocks(self):
    return self.blocks
    
  def get_module_list(self):
    return self.module_list

                
  def forward(self, x):
      global CUDA
      CUDA = False
     # global gllayer
      detections = []
      modules = self.blocks[1:]
      outputs = {}   #We cache the outputs for the route layer
        
        
      write = 0
      for i,module in enumerate(modules):        
            
        module_type = (modules[i]["type"])
        if module_type == "convolutional" or module_type == "upsample" or module_type == "maxpool":
                
          x = self.module_list[i](x)
          outputs[i] = x
        elif module_type == "route":
          
          layers = module["layers"]
          
          layers= list(map(int,layers))
          self.gllayer.append(layers)
              
          if (layers[0]) > 0:
               layers[0] = layers[0] - i

          if len(layers) == 1:
               x = outputs[i + (layers[0])]

          else:
            if (layers[1]) > 0:
                layers[1] = layers[1] - i

            map1 = outputs[i + layers[0]]
            map2 = outputs[i + layers[1]]


            x = torch.cat((map1, map2), 1)
            outputs[i] = x
            
        elif  module_type == "shortcut":
            from_ = int(module["from"])
            x = outputs[i-1] + outputs[i+from_]
            outputs[i]=x
        elif module_type == 'yolo':        
                
                anchors = self.module_list[i][0].anchors
                #Get the input dimensions
                inp_dim = int (self.net_info["height"])
                
                #Get the number of classes
                num_classes = int (module["classes"])
                
                #Output the result
                x = x.data
                x = predict_transform(x, inp_dim, anchors, num_classes,CUDA)
                
                if type(x) == 'int':
                    continue

                
                if not write:
                    detections = x
                    write = 1
                
                else:
                    detections = torch.cat((detections, x), 1)
                
                outputs[i] = outputs[i-1]
                
        
        
        
      return detections
    
    
  def load_weights(self, weightfile):
        
        #Open the weights file
        fp = open(weightfile, "rb")

        #The first 4 values are header information 
        # 1. Major version number
        # 2. Minor Version Number
        # 3. Subversion number 
        # 4. IMages seen 
        header = np.fromfile(fp, dtype = np.int32, count = 5)
        self.header = torch.from_numpy(header)
        self.seen = self.header[3]
        
        #The rest of the values are the weights
        # Let's load them up
        weights = np.fromfile(fp, dtype = np.float32)
        
        ptr = 0
        for i in range(len(self.module_list)):
            module_type = self.blocks[i + 1]["type"]
            
            if module_type == "convolutional":
                model = self.module_list[i]
                try:
                    batch_normalize = int(self.blocks[i+1]["batch_normalize"])
                except:
                    batch_normalize = 0
                
                conv = model[0]
                
                if (batch_normalize):
                    bn = model[1]
                    
                    #Get the number of weights of Batch Norm Layer
                    num_bn_biases = bn.bias.numel()
                    
                    #Load the weights
                    bn_biases = torch.from_numpy(weights[ptr:ptr + num_bn_biases])
                    ptr += num_bn_biases
                    
                    bn_weights = torch.from_numpy(weights[ptr: ptr + num_bn_biases])
                    ptr  += num_bn_biases
                    
                    bn_running_mean = torch.from_numpy(weights[ptr: ptr + num_bn_biases])
                    ptr  += num_bn_biases
                    
                    bn_running_var = torch.from_numpy(weights[ptr: ptr + num_bn_biases])
                    ptr  += num_bn_biases
                    
                    #Cast the loaded weights into dims of model weights. 
                    bn_biases = bn_biases.view_as(bn.bias.data)
                    bn_weights = bn_weights.view_as(bn.weight.data)
                    bn_running_mean = bn_running_mean.view_as(bn.running_mean)
                    bn_running_var = bn_running_var.view_as(bn.running_var)

                    #Copy the data to model
                    bn.bias.data.copy_(bn_biases)
                    bn.weight.data.copy_(bn_weights)
                    bn.running_mean.copy_(bn_running_mean)
                    bn.running_var.copy_(bn_running_var)
                
                else:
                    #Number of biases
                    num_biases = conv.bias.numel()
                
                    #Load the weights
                    conv_biases = torch.from_numpy(weights[ptr: ptr + num_biases])
                    ptr = ptr + num_biases
                    
                    #reshape the loaded weights according to the dims of the model weights
                    conv_biases = conv_biases.view_as(conv.bias.data)
                    
                    #Finally copy the data
                    conv.bias.data.copy_(conv_biases)
                    
                    
                #Let us load the weights for the Convolutional layers
                num_weights = conv.weight.numel()
                
                #Do the same as above for weights
                conv_weights = torch.from_numpy(weights[ptr:ptr+num_weights])
                ptr = ptr + num_weights

                conv_weights = conv_weights.view_as(conv.weight.data)
                conv.weight.data.copy_(conv_weights)

#####----------------test code to return tensor of output of image forwarded to the network-------------##########
def get_test_input():
    img = cv2.imread("beat.png")
    img = cv2.resize(img, (608,608))   
    #img = cv2.cvtColor(img,cv2.COLOR_BGR2RGB)   #Resize to the input dimension
    img_ =  img[:,:,::-1].transpose((2,0,1))  # BGR -> RGB | H X W C -> C X H X W 
    img_ = img_[np.newaxis,:,:,:]/255.0       #Add a channel at 0 (for batch) | Normalise
    img_ = torch.from_numpy(img_).float()     #Convert to float
    img_ = Variable(img_)                     # Convert to Variable
    return img_

########--------------Function to take in output tensor and return prediction-----------######      
def predict_transform(prediction, inp_dim, anchors, num_classes,CUDA=False):
    batch_size = prediction.size(0)
    stride =  inp_dim // prediction.size(2)
    grid_size = inp_dim // stride
    bbox_attrs = 5 + num_classes
    num_anchors = len(anchors)
    
    anchors = [(a[0]/stride, a[1]/stride) for a in anchors]



    prediction = prediction.view(batch_size, bbox_attrs*num_anchors, grid_size*grid_size)
    prediction = prediction.transpose(1,2).contiguous()
    prediction = prediction.view(batch_size, grid_size*grid_size*num_anchors, bbox_attrs)


    #Sigmoid the  centre_X, centre_Y. and object confidencce
    prediction[:,:,0] = torch.sigmoid(prediction[:,:,0])
    prediction[:,:,1] = torch.sigmoid(prediction[:,:,1])
    prediction[:,:,4] = torch.sigmoid(prediction[:,:,4])
    

    
    #Add the center offsets
    grid_len = np.arange(grid_size)
    a,b = np.meshgrid(grid_len, grid_len)
    
    x_offset = torch.FloatTensor(a).view(-1,1)
    y_offset = torch.FloatTensor(b).view(-1,1)
    
    #if CUDA:
       # x_offset = x_offset.cuda()
        #y_offset = y_offset.cuda()
    
    x_y_offset = torch.cat((x_offset, y_offset), 1).repeat(1,num_anchors).view(-1,2).unsqueeze(0)
    
    prediction[:,:,:2] += x_y_offset
      
    #log space transform height and the width
    anchors = torch.FloatTensor(anchors)
    
    #if CUDA:
       # anchors = anchors.cuda()
    
    anchors = anchors.repeat(grid_size*grid_size, 1).unsqueeze(0)
    prediction[:,:,2:4] = torch.exp(prediction[:,:,2:4])*anchors

    #Softmax the class scores
    prediction[:,:,5: 5 + num_classes] = torch.sigmoid((prediction[:,:, 5 : 5 + num_classes]))

    prediction[:,:,:4] *= stride
    return prediction

########------------Function to return IoU of two bounding boxes-------------------#########
def bbox_iou(box1, box2):
   
    #Get the coordinates of bounding boxes
    b1_x1, b1_y1, b1_x2, b1_y2 = box1[:,0], box1[:,1], box1[:,2], box1[:,3]
    b2_x1, b2_y1, b2_x2, b2_y2 = box2[:,0], box2[:,1], box2[:,2], box2[:,3]
    
    #get the corrdinates of the intersection rectangle
    inter_rect_x1 =  torch.max(b1_x1, b2_x1)
    inter_rect_y1 =  torch.max(b1_y1, b2_y1)
    inter_rect_x2 =  torch.min(b1_x2, b2_x2)
    inter_rect_y2 =  torch.min(b1_y2, b2_y2)
    
    #Intersection area
    #if torch.cuda.is_available():
           # inter_area = torch.max(inter_rect_x2 - inter_rect_x1 + 1,torch.zeros(inter_rect_x2.shape).cuda())*torch.max(inter_rect_y2 - inter_rect_y1 + 1, torch.zeros(inter_rect_x2.shape).cuda())
    
    inter_area = torch.max(inter_rect_x2 - inter_rect_x1 + 1,torch.zeros(inter_rect_x2.shape))*torch.max(inter_rect_y2 - inter_rect_y1 + 1, torch.zeros(inter_rect_x2.shape))
    
    #Union Area
    b1_area = (b1_x2 - b1_x1 + 1)*(b1_y2 - b1_y1 + 1)
    b2_area = (b2_x2 - b2_x1 + 1)*(b2_y2 - b2_y1 + 1)
    
    iou = inter_area / (b1_area + b2_area - inter_area)
    
    return iou

######## ---Function to differentiate between various boxes with same prediction--------########
def unique(tensor):
    tensor_np = tensor.cpu().numpy()
    unique_np = np.unique(tensor_np)
    unique_tensor = torch.from_numpy(unique_np)
    
    tensor_res = tensor.new(unique_tensor.shape)
    tensor_res.copy_(unique_tensor)
    return tensor_res

###############----------------------------Function to Load class-------------------########################  
def load_classes(namesfile):
    fp = open(namesfile, "r")
    names = fp.read().split("\n")[:-1]
    return names

##############------------------------Prerocessing image to resizee image with unchanged aspect Ratio using padding------------------################
def letterbox_image(img, inp_dim):
    
    img_w, img_h = img.shape[1], img.shape[0]
    w, h = inp_dim
    new_w = int(img_w * min(w/img_w, h/img_h))
    new_h = int(img_h * min(w/img_w, h/img_h))
    resized_image = cv2.resize(img, (new_w,new_h), interpolation = cv2.INTER_CUBIC)
    
    canvas = np.full((inp_dim[1], inp_dim[0], 3), 128)

    canvas[(h-new_h)//2:(h-new_h)//2 + new_h,(w-new_w)//2:(w-new_w)//2 + new_w,  :] = resized_image
    
    return canvas
###########------------------Prepare image for inputting to Neural Network--------------------##################
def prep_image(img, inp_dim):
    
    img = cv2.resize(img, (inp_dim, inp_dim))
    img = img[:,:,::-1].transpose((2,0,1)).copy()
    img = torch.from_numpy(img).float().div(255.0).unsqueeze(0)
    return img

########------------------------subject our output to objectness score thresholding and Non-maximal suppression, to obtain True Detetction-------------####
def write_results(prediction, confidence, num_classes, nms = True, nms_conf = 0.4):
    conf_mask = (prediction[:,:,4] > confidence).float().unsqueeze(2)
    prediction = prediction*conf_mask
    

    try:
        ind_nz = torch.nonzero(prediction[:,:,4]).transpose(0,1).contiguous()
    except:
        return 0
    
    
    box_a = prediction.new(prediction.shape)
    box_a[:,:,0] = (prediction[:,:,0] - prediction[:,:,2]/2)
    box_a[:,:,1] = (prediction[:,:,1] - prediction[:,:,3]/2)
    box_a[:,:,2] = (prediction[:,:,0] + prediction[:,:,2]/2) 
    box_a[:,:,3] = (prediction[:,:,1] + prediction[:,:,3]/2)
    prediction[:,:,:4] = box_a[:,:,:4]
    

    
    batch_size = prediction.size(0)
    
    output = prediction.new(1, prediction.size(2) + 1)
    write = False


    for ind in range(batch_size):
        #select the image from the batch
        image_pred = prediction[ind]
        

        
        #Get the class having maximum score, and the index of that class
        #Get rid of num_classes softmax scores 
        #Add the class index and the class score of class having maximum score
        max_conf, max_conf_score = torch.max(image_pred[:,5:5+ num_classes], 1)
        max_conf = max_conf.float().unsqueeze(1)
        max_conf_score = max_conf_score.float().unsqueeze(1)
        seq = (image_pred[:,:5], max_conf, max_conf_score)
        image_pred = torch.cat(seq, 1)
        

        
        #Get rid of the zero entries
        non_zero_ind =  (torch.nonzero(image_pred[:,4]))

        
        image_pred_ = image_pred[non_zero_ind.squeeze(),:].view(-1,7)
        
        #Get the various classes detected in the image
        try:
            img_classes = unique(image_pred_[:,-1])
        except:
             continue
        #WE will do NMS classwise
        for cls in img_classes:
            #get the detections with one particular class
            cls_mask = image_pred_*(image_pred_[:,-1] == cls).float().unsqueeze(1)
            class_mask_ind = torch.nonzero(cls_mask[:,-2]).squeeze()
            

            image_pred_class = image_pred_[class_mask_ind].view(-1,7)

		
        
             #sort the detections such that the entry with the maximum objectness
             #confidence is at the top
            conf_sort_index = torch.sort(image_pred_class[:,4], descending = True )[1]
            image_pred_class = image_pred_class[conf_sort_index]
            idx = image_pred_class.size(0)
            
            #if nms has to be done
            if nms:
                #For each detection
                for i in range(idx):
                    #Get the IOUs of all boxes that come after the one we are looking at 
                    #in the loop
                    try:
                        ious = bbox_iou(image_pred_class[i].unsqueeze(0), image_pred_class[i+1:])
                    except ValueError:
                        break
        
                    except IndexError:
                        break
                    
                    #Zero out all the detections that have IoU > treshhold
                    iou_mask = (ious < nms_conf).float().unsqueeze(1)
                    image_pred_class[i+1:] *= iou_mask       
                    
                    #Remove the non-zero entries
                    non_zero_ind = torch.nonzero(image_pred_class[:,4]).squeeze()
                    image_pred_class = image_pred_class[non_zero_ind].view(-1,7)
                    
                    

            #Concatenate the batch_id of the image to the detection
            #this helps us identify which image does the detection correspond to 
            #We use a linear straucture to hold ALL the detections from the batch
            #the batch_dim is flattened
            #batch is identified by extra batch column
            
            
            batch_ind = image_pred_class.new(image_pred_class.size(0), 1).fill_(ind)
            seq = batch_ind, image_pred_class
            if not write:
                output = torch.cat(seq,1)
                write = True
            else:
                out = torch.cat(seq,1)
                output = torch.cat((output,out))
    
    return output
##################----------------------Function to draw The boxes------------------######################
def writeim(x,results):
        c1 = tuple(x[1:3].int())
        c2 = tuple(x[3:5].int())
        img = results[int(x[0])]
        cls = int(x[-1])
        label = "{0}".format(classes[cls])
        color = (random.randint(0,256),random.randint(0,256),random.randint(0,256))
        cv2.rectangle(img, c1, c2,color, 3)
        t_size = cv2.getTextSize(label, cv2.FONT_HERSHEY_PLAIN, 1 , 1)[0]
        c2 = c1[0] + t_size[0] + 3, c1[1] + t_size[1] + 4
        cv2.rectangle(img, c1, c2,color, 3)
        cv2.putText(img, label, (c1[0], c1[1] + t_size[1] + 4), cv2.FONT_HERSHEY_PLAIN, 1, [225,255,255], 2)
        return img

#####-------------------------------IMAGE DETECTION-----------------------------------#####
images = 'beat.png'
batch_size =1
confidence =0.5
nms_thesh = 0.4
start = 0
CUDA = torch.cuda.is_available()
print("Loading network.....")
classes = load_classes("coco.names")
try:
    imlist = [osp.join(osp.realpath('.'), images, img) for img in os.listdir(images)]
except NotADirectoryError:
    imlist = []
    imlist.append(osp.join(osp.realpath('.'), images))
except FileNotFoundError:
    print ("No file or directory with the name {}".format(images))
    exit()
model = Darknet('yolov3.cfg')
model.load_weights('yolov3.weights')
print("Network successfully loaded")

model.net_info["height"] = 416
inp_dim = int(model.net_info["height"])
assert inp_dim % 32 == 0 
assert inp_dim > 32


#Set the model in evaluation mode
model.eval()
loaded_ims = [cv2.imread(x) for x in imlist]
im_batches = list(map(prep_image, loaded_ims, [inp_dim for x in range(len(imlist))]))

#List containing dimensions of original images
im_dim_list = [(x.shape[1], x.shape[0]) for x in loaded_ims]
im_dim_list = torch.FloatTensor(im_dim_list).repeat(1,2)
leftover = 0
if (len(im_dim_list) % batch_size):
   leftover = 1

if batch_size != 1:
  num_batches = len(imlist) // batch_size + leftover            
  im_batches = [torch.cat((im_batches[i*batch_size : min((i +  1)*batch_size,
                       len(im_batches))]))  for i in range(num_batches)]  
write = 0
start_det_loop = time.time()
for i, batch in enumerate(im_batches):
    #load the image 
    

    prediction = model(Variable(batch, volatile = True))

    prediction = write_results(prediction, confidence, num_classes, nms_conf = nms_thesh)

    end = time.time()

    if type(prediction) == int:

        for im_num, image in enumerate(imlist[i*batch_size: min((i +  1)*batch_size, len(imlist))]):
            im_id = i*batch_size + im_num
            print("{0:20s} predicted in {1:6.3f} seconds".format(image.split("/")[-1], (end - start)/batch_size))
            print("{0:20s} {1:s}".format("Objects Detected:", ""))
            print("----------------------------------------------------------")
        continue

    prediction[:,0] += i*batch_size    #transform the atribute from index in batch to index in imlist 

    if not write:                      #If we have't initialised output
        output = prediction  
        write = 1
    else:
        output = torch.cat((output,prediction))

    for im_num, image in enumerate(imlist[i*batch_size: min((i +  1)*batch_size, len(imlist))]):
        im_id = i*batch_size + im_num
        objs = [classes[int(x[-1])] for x in output if int(x[0]) == im_id]
try:
    output
except NameError:
    print ("No detections were made")
    exit()
im_dim_list = torch.index_select(im_dim_list, 0, output[:,0].long())

scaling_factor = torch.min(inp_dim/im_dim_list,1)[0].view(-1,1)


output[:,[1,3]] -= (inp_dim - scaling_factor*im_dim_list[:,0].view(-1,1))/2
output[:,[2,4]] -= (inp_dim - scaling_factor*im_dim_list[:,1].view(-1,1))/2
output[:,1:5] /= scaling_factor
for i in range(output.shape[0]):
    output[i, [1,3]] = torch.clamp(output[i, [1,3]], 0.0, im_dim_list[i,0])
    output[i, [2,4]] = torch.clamp(output[i, [2,4]], 0.0, im_dim_list[i,1])

prediction[0][0][]

######----------------------Image Printing--------------########################
tim=list(map(lambda x: writeim(x, loaded_ims), output))
det_names = pd.Series(imlist).apply(lambda x: "{}/det_{}".format('det',x.split("/")[-1]))
list(map(cv2.imwrite, det_names, loaded_ims))
from google.colab.patches import cv2_imshow
cv2_imshow(tim[2])

###################---------------------------------------VIDEO DETETCION---------------------------------------------#########
batch_size =1
confidence =0.5
nms_thesh = 0.4
start = 0
CUDA = torch.cuda.is_available()
cap = cv2.VideoCapture('UHD Ultra HD 4K Video Stock Footage Dubai Busy City Street Highway Freeway Traffic Rush Hour Day.mp4')
print("Loading network.....")
model = Darknet('yolov3.cfg')
model.load_weights('yolov3.weights')
print("Network successfully loaded")
classes = load_classes("coco.names")

model.net_info["height"] = 416
inp_dim = int(model.net_info["height"])
assert inp_dim % 32 == 0 
assert inp_dim > 32


#Set the model in evaluation mode
model.eval()
fourcc=cv2.VideoWriter_fourcc(*'XVID')
width = int(cap.get(3))   # float
height = int(cap.get(4))
out=cv2.VideoWriter('output1.avi',fourcc,20.0,(width,height))
x =0

while(True):
    # Capture frame-by-frame
    loaded_ims=[]
    ret, images = cap.read()
    if ret==False:
      break
    x=x+1

    # Our operations on the frame come here
    #gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

    # Display the resulting frame
    loaded_ims.append(images)
    #print(loaded_ims)
    im_batches = list(map(prep_image, loaded_ims, [inp_dim for x in range(len(loaded_ims))]))
    

    #List containing dimensions of original images
    im_dim_list = [(x.shape[1], x.shape[0]) for x in loaded_ims]
    im_dim_list = torch.FloatTensor(im_dim_list).repeat(1,2)
    leftover = 0
    if (len(im_dim_list) % batch_size):
       leftover = 1

    if batch_size != 1:
      num_batches = len(imlist) // batch_size + leftover            
      im_batches = [torch.cat((im_batches[i*batch_size : min((i +  1)*batch_size,
                           len(im_batches))]))  for i in range(num_batches)]  
    write = 0
    start_det_loop = time.time()
    for i, batch in enumerate(im_batches):
        #load the image 


        prediction = model(Variable(batch, volatile = True))

        prediction = write_results(prediction, confidence, num_classes, nms_conf = nms_thesh)

        end = time.time()

        if type(prediction) == int:

            for im_num, image in enumerate(loaded_ims[i*batch_size: min((i +  1)*batch_size, len(loaded_ims))]):
                im_id = i*batch_size + im_num
                print("{0:20s} predicted in {1:6.3f} seconds".format(image.split("/")[-1], (end - start)/batch_size))
                print("{0:20s} {1:s}".format("Objects Detected:", ""))
                print("----------------------------------------------------------")
            continue

        prediction[:,0] += i*batch_size    #transform the atribute from index in batch to index in imlist 

        if not write:                      #If we have't initialised output
            output = prediction  
            write = 1
        else:
            output = torch.cat((output,prediction))

        for im_num, image in enumerate(loaded_ims[i*batch_size: min((i +  1)*batch_size, len(loaded_ims))]):
            im_id = i*batch_size + im_num
            objs = [classes[int(x[-1])] for x in output if int(x[0]) == im_id]
    try:
        output
    except NameError:
        print ("No detections were made")
        exit()
    im_dim_list = torch.index_select(im_dim_list, 0, output[:,0].long())

    scaling_factor = torch.min(inp_dim/im_dim_list,1)[0].view(-1,1)


    output[:,[1,3]] -= (inp_dim - scaling_factor*im_dim_list[:,0].view(-1,1))/2
    output[:,[2,4]] -= (inp_dim - scaling_factor*im_dim_list[:,1].view(-1,1))/2
    output[:,1:5] /= scaling_factor
    for i in range(output.shape[0]):
        output[i, [1,3]] = torch.clamp(output[i, [1,3]], 0.0, im_dim_list[i,0])
        output[i, [2,4]] = torch.clamp(output[i, [2,4]], 0.0, im_dim_list[i,1])
    tim=list(map(lambda x: writeim(x, loaded_ims), output))
    #det_names = pd.Series(loaded_ims).apply(lambda x: "{}/det_{}".format('det',x.split("/")[-1]))
    #list(map(cv2.imwrite, det_names, loaded_ims))


    print(x)
    out.write(tim[0])
    if cv2.waitKey(1) & 0xFF == ord('q'):
        break

# When everything done, release the capture
cap.release()