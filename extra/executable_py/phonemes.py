""""Script to preprocess data and train a phoneme model using DeepPhonemizer."""
import logging.config
from pathlib import Path

import torch
import torch.multiprocessing as mp
from dp.phonemizer import Phonemizer
from dp.preprocess import preprocess
from dp.train import train
from dp.utils.io import read_config

config_file_path = Path('logging.yaml')
config = read_config(config_file_path)
logging.config.dictConfig(config)

if __name__ == '__main__':

    train_data = [('nl','pad','pɑt'), ('nl','kat','kɑt'),('nl','tak','tɑk'),('nl','tja','tɕa'), ('nl','fiets','fits'),\
        ('nl','huis','ɦœys'), ('nl','boom','bom'), ('nl','straat','straːt'), ('nl','school','sxoːl'),('nl','vriend','vrint'),\
        ('nl','licht','lɪxt'), ('nl','dag','dɑx'), ('nl','zee','zeː'), ('nl','water','ʋaːtər'), ('nl','eten','eːtən'), ('nl','drinken','drɪŋkən'),\
        ('nl','lopen','loːpən'), ('nl','slapen','slaːpən'), ('nl','werken','ʋɛrkən'), ('nl','spelen','speːlən'), ('nl','lezen','leːzən'),\
        ('nl','schrijven','sxrɛivən'), ('nl','zingen','zɪŋən'), ('nl','dansen','dɑnsən'), ('nl','zingen','zɪŋən'), ('nl','rennen','rɛnən'), ('nl','zwemmen','zʋɛmən'),\
        ('nl','vliegen','vliːɣən'), ('nl','rijden','rɛidən'), ('nl','kijken','kɛikən'), ('nl','horen','ɦoːrən'), ('nl','voelen','vuːlən'), ('nl','denken','dɛŋkən'), \
        ('nl','begrijpen','bəɣrɛipən'), ('nl','weten','ʋeːtən'), ('nl','begraven','bəɣraːvən'), ('nl','vergeten','fərɣeːtən'), ('nl','verliezen','fərliːzən'),\
        ('nl','vinden','vɪndən'), ('nl','houden','ɦʌudən'), ('nl','kopen','koːpən'), ('nl','verkopen','fərkoːpən'), ('nl','betalen','bətaːlən'), ('nl','werken','ʋɛrkən'),\
        ('nl','studeren','stydɛrən'), ('nl','leren','leːrən'), ('nl','begrijpen','bəɣrɛipən'), ('nl','uitleggen','œytlɛxən'),\
        ('nl','vertellen','fərtɛlən'), ('nl','lezen','leːzən'), ('nl','schrijven','sxrɛivən'), ('nl','luisteren','lœystərən'), ('nl','kijken','kɛikən'), \
        ('nl','zien','zin'), ('nl','horen','ɦoːrən'),('nl','kool','koːl'),('nl','stoel','stul'),('n','feit','fɛit'),('nl','neus','nøys'),('nl','muis','mœys'),\
        ('nl','paard','paːrt'),('nl','vork','fɔrk'),('nl','mes','mɛs'),('nl','lepel','lepəl'),('nl','bord','bɔrt'),('nl','glas','ɣlɑs'),('nl','beker','bekər'),\
        ('nl','tafel','taːfəl'),('nl','stoel','stul'),('nl','bed','bɛt'),('nl','kussen','kʏsən'),('nl','deken','deːkən'),\
        ('nl','lamp','lɑmp'),('nl','deur','døːr'),('nl','raam','raːm'),('nl','vloer','vloːr'),('nl','muur','myːr'), ('nl','plafond','plaːfɔnt'),\
        ('nl','tuin','tœyn'),('nl','boom','bom'),('nl','bloem','blum'),('nl','gras','ɣrɑs'),('nl','struik','strœyk'),('nl','blad','blɑt'),('nl','wortel','ʋɔrtəl'),\
        ('nl','stam','stɑm'),('nl','sjaals','ɕɑ:ls'),('nl','tak','tɑk'),('nl','wortel','ʋɔrtəl')]


    val_data = [('nl', 'acht', 'axt'), ('nl', 'fiets', 'fits'), ('nl', 'school', 'sxoːl'),
                ('nl', 'benutten', 'bənutten̩')] * 100

    config_file = 'dp/configs/forward_config.yaml'

    preprocess(config_file=config_file,
               train_data=train_data,
               val_data=val_data,
               deduplicate_train_data=False)

    num_gpus = torch.cuda.device_count()

    if num_gpus > 1:
        mp.spawn(train, nprocs=num_gpus, args=(num_gpus, config_file))
    else:
        train(rank=0, num_gpus=num_gpus, config_file=config_file)

