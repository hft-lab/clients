from clients.hitbtc import HitbtcClient
from clients.bibox import BiboxClient
from clients.bitmake import BitmakeClient
from clients.globederivative import GlobeClient
from clients.bit import BitClient
from clients.btse import BtseClient

ALL_CLIENTS = {
    'HITBTC': HitbtcClient,
    'BIBOX': BiboxClient,
    'BITMAKE': BitmakeClient,
    'GLOBE': GlobeClient,
    'BIT': BitClient,
    'BTSE': BtseClient
}
