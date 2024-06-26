import socketserver
import matplotlib.pyplot as plt
import numpy as np
import torch
import pandas as pd
import pickle
import torch.nn.functional as F
from os.path import exists
from dataloader.dataset import FSLDataset
from runner.utils import get_config, extract_test_sample
from model.vit import ViT
import runner.proto as proto

config = get_config('config.yaml')
# use_cuda = config['GPU']['cuda']

HOST = config['application']['server']['ip']
PORT = config['application']['server']['port']

mac = config['application']['client']['mac']

global P_COUNT
P_COUNT = 0
window_size = config['FSL']['dataset']['window_size']
num_sub = config['subcarrier'][config['application']['client']["bandwidth"]]
activities = config['application']['client']["activity_labels"]

columns = []
for i in range(0, num_sub):
    columns.append('_' + str(i))

# 64에 대해서만 처리중.. 추후 40MHz 이상에 대한 널 처리를 해줘야함
null_pilot_col_list = ['_' + str(x + 32) for x in [-32, -31, -30, -29, -21, -7, 0, 7, 21, 29, 30, 31]]

# Load pretrained model
print('======> Load model')
model = proto.load_protonet_vit(
        in_channels=config['application']['model']['ViT']["in_channels"],
        patch_size=(config['application']['model']['ViT']["patch_size"], config['subcarrier'][config['application']['client']["bandwidth"]]),
        embed_dim=config['application']['model']['ViT']["embed_dim"],
        num_layers=config['application']['model']['ViT']["num_layers"],
        num_heads=config['application']['model']['ViT']["num_heads"],
        mlp_dim=config['application']['model']['ViT']["mlp_dim"],
        num_classes=len(config['application']['client']["activity_labels"]),
        in_size=[config['application']['client']["window_size"], config['subcarrier'][config['application']['client']["bandwidth"]]]
        )

model.load_state_dict(torch.load(config['application']['FSL']['save_model_path']))

# if use_cuda:
#     model.to(config['GPU']['gpu_ids'][0])
print('======> Success')


# Create Prototypes before process real-time CSI
print('======> Create Prototypes')
n_way = config['FSL']['test']['n_way']
n_support = config['FSL']['test']['n_support']
n_query = config['FSL']['test']['n_query']

support_data = FSLDataset(config['FSL']['dataset']['test_dataset_path'],
                          win_size=window_size,
                          mode='test',
                          mac=False, time=False
                          )
support_x, support_y = support_data.data_x, support_data.data_y
support_x = np.expand_dims(support_x, axis=1)
support_sample = extract_test_sample(n_way, n_support, n_query, support_x, support_y, config)
z_proto = model.create_protoNet(support_sample)
print('======> Success')

mac_dict = {}
mac_dict[mac] = pd.DataFrame(columns=columns)
# mac_dict[mac].drop(null_pilot_col_list, axis=1, inplace=True)

class MyTcpHandler(socketserver.BaseRequestHandler):

    def handle(self):
        # global use_cuda
        # print('{0} is connected'.format(self.client_address[0]))
        buffer = self.request.recv(2048)  # receive data
        buffer = pickle.loads(buffer)
        global P_COUNT
        P_COUNT += 1

        if not buffer:
            print("Fail to receive!")
            return
        else:
            csi_df = pd.DataFrame([buffer], columns=columns)

            '''
                1. Remove null & pilot subcarrier
                2. Keep window_size 50. If 25 packets changed, choose 1 subcarrier and run model.
            '''
            # 1. Remove null & pilot subcarrier
            # csi_df.drop(null_pilot_col_list, axis=1, inplace=True)

            # 2. Keeping window_size. If half packets changed, choose 1 subcarrier and run model
            try:
                mac_dict[mac] = pd.concat([mac_dict[mac], csi_df], ignore_index=True)
                if len(mac_dict[mac]) == window_size and P_COUNT == window_size:
                    c_data = np.array(mac_dict[mac])

                    c_data = torch.from_numpy(c_data).unsqueeze(0).float()

                    with torch.no_grad():
                        output = model.proto_test(c_data, z_proto, n_way, 0)
                        y_hat = output['y_hat']

                    print('Predict result: {}'.format(activities[y_hat.item()]))

                    # Drop first row
                    mac_dict[mac].drop(0, inplace=True)
                    mac_dict[mac].reset_index(drop=True, inplace=True)

                    P_COUNT = 0

                elif len(mac_dict[mac]) == window_size and P_COUNT == window_size // 2:
                    c_data = np.array(mac_dict[mac])
                    # c_data shape: [1, 1, window_size, num_subcarriers]
                    c_data = torch.from_numpy(c_data).unsqueeze(0).float()

                    with torch.no_grad():
                        output = model.proto_test(c_data, z_proto, n_way, 0)
                        y_hat = output['y_hat']

                    print('Predict result: {}'.format(activities[y_hat.item()]))

                    # Drop first row
                    mac_dict[mac].drop(0, inplace=True)
                    mac_dict[mac].reset_index(drop=True, inplace=True)

                    P_COUNT = 0

                elif len(mac_dict[mac]) == window_size:
                    # Drop first row
                    mac_dict[mac].drop(0, inplace=True)
                    mac_dict[mac].reset_index(drop=True, inplace=True)

                elif len(mac_dict[mac]) > window_size:
                    print("Error!")

            except Exception as e:
                print('Error', e)


def runServer(HOST, PORT):
    print('==== Start Edge Server ====')
    print('==== Exit with Ctrl + C ====')

    try:
        server = socketserver.TCPServer((HOST, PORT), MyTcpHandler)
        server.serve_forever()  # server_forever()메소드를 호출하면 클라이언트의 접속 요청을 받을 수 있음

    except KeyboardInterrupt:
        print('==== Exit Edge server ====')


if __name__ == '__main__':
    runServer(HOST, PORT)
