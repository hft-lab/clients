import time
from datetime import datetime
import requests
# import clients.btse_whitebit_speed_test

headers = {"Accept": "application/json;charset=UTF-8",
           "Content-Type": "application/json",
           'Connection': 'keep-alive'}


def get_orderbook_btse(session):
    path = 'https://api.btse.com/futures/api/v2.1/orderbook'
    params = {'symbol': 'ETHPFC', 'depth': 10}
    post_string = '?' + "&".join([f"{key}={params[key]}" for key in sorted(params)])
    # headers = get_private_headers(path, {})  # Assuming authentication is required
    res = session.get(url=path + post_string, data=params)
    # print('BTSE', res.headers)
    ob = res.json()
    if 'buyQuote' in ob and 'sellQuote' in ob:
        orderbook = {
            'timestamp': ob['timestamp'],
            'asks': [[float(ask['price']), float(ask['size'])] for ask in ob['sellQuote']],
            'bids': [[float(bid['price']), float(bid['size'])] for bid in ob['buyQuote']]
        }
        return orderbook


def get_orderbook_whitebit(session):
    path = 'https://whitebit.com/api/v4/public/orderbook/BTC_PERP'
    params = {'limit': 10}  # Adjusting parameters as per WhiteBit's documentation
    data = ''
    strl = []
    for key in sorted(params):
        strl.append(f'{key}={params[key]}')
    data += '&'.join(strl)
    path += f'?{data}'.replace(' ', '%20')
    resp = session.get(url=path)
    # print('whitebit', resp.headers)
    ob = resp.json()
    # Check if the response is a dictionary and has 'asks' and 'bids' directly within it
    if isinstance(ob, dict) and 'asks' in ob and 'bids' in ob:
        orderbook = {
            'asks': [[float(ask[0]), float(ask[1])] for ask in ob['asks']],
            'bids': [[float(bid[0]), float(bid[1])] for bid in ob['bids']],
            'timestamp': datetime.utcnow().timestamp()
        }
        return orderbook


if __name__ == '__main__':
    whitebit_records = []
    btse_records = []
    session = requests.Session()
    session.headers.update(headers)
    while True:
        time.sleep(1)
        time_start_whitebit = datetime.utcnow().timestamp()
        ob_whitebit_time = get_orderbook_whitebit(session)['timestamp']
        # print(f"GET OB WHITEBIT TIME: {ob_whitebit_time - time_start_whitebit} sec")
        whitebit_records.append(ob_whitebit_time - time_start_whitebit)
        print(f"GET OB WHITEBIT AV. TIME: {sum(whitebit_records) / len(whitebit_records)} sec")
        time_start_btse = datetime.utcnow().timestamp()
        ob_btse_time = get_orderbook_btse(session)['timestamp'] / 1000
        # print(f"GET OB BTSE TIME: {ob_btse_time - time_start_btse} sec")
        btse_records.append(datetime.utcnow().timestamp() - time_start_btse)
        print(f"GET OB BTSE AV. TIME: {sum(btse_records) / len(btse_records)} sec")
        print()
