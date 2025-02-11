# 数据集划分
import os
import random

root_dir = '/home/wxq/od/1/RDD2022/'

## 0.7train 0.1val 0.2test
trainval_percent = 0.8
train_percent = 0.7
xmlfilepath = root_dir + 'Annotations'
txtsavepath = root_dir + 'ImageSets/Main'
total_xml = os.listdir(xmlfilepath)

num = len(total_xml)  # 100
list = range(num)
tv = int(num * trainval_percent)  # 80
tr = int(tv * train_percent)  # 80*0.7=56
trainval = random.sample(list, tv)
train = random.sample(trainval, tr)

ftrainval = open(root_dir + 'ImageSets/Main/trainval.txt', 'w')
ftest = open(root_dir + 'ImageSets/Main/test.txt', 'w')
ftrain = open(root_dir + 'ImageSets/Main/train.txt', 'w')
fval = open(root_dir + 'ImageSets/Main/val.txt', 'w')

for i in list:
    name = total_xml[i][:-4] + '\n'
    if i in trainval:
        ftrainval.write(name)
        if i in train:
            ftrain.write(name)
        else:
            fval.write(name)
    else:
        ftest.write(name)

ftrainval.close()
ftrain.close()
fval.close()
ftest.close()