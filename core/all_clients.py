from clients.apollox import ApolloxClient
from clients.binance import BinanceClient
from clients.bitmex import BitmexClient
from clients.dydx import DydxClient
from clients.kraken import KrakenClient
from clients.okx import OkxClient
<<<<<<< Updated upstream
from clients.hitbtc import HitbtcClient
from clients.bibox import BiboxClient
from clients.bitmake import BitmakeClient
from clients.globederivative import GlobeClient
from clients.bit import BitClient
from clients.btse import BtseClient
from clients.whitebit import WhiteBit
=======

>>>>>>> Stashed changes

ALL_CLIENTS = {
    'BITMEX': BitmexClient,
    'DYDX': DydxClient,
    'BINANCE': BinanceClient,
    'APOLLOX': ApolloxClient,
    'OKX': OkxClient,
<<<<<<< Updated upstream
    'KRAKEN': KrakenClient,
    'HITBTC': HitbtcClient,
    'BIBOX': BiboxClient,
    'BITMAKE': BitmakeClient,
    'GLOBE': GlobeClient,
    'BIT': BitClient,
    'BTSE': BtseClient,
    'WHITEBIT': WhiteBit
=======
    'KRAKEN': KrakenClient
>>>>>>> Stashed changes
}
