import os
import re
import time
import pyautogui
import collections
import json
import requests
import random
import math
import cloudscraper

from datetime import datetime
from jinja2 import Environment, PackageLoader, select_autoescape
from operator import itemgetter
from file_read_backwards import FileReadBackwards
from queue import Queue

from modules.base import Base, OCRChecker
from modules.db import TradeDB
from modules.keys import KeyActions


class Prices(Base):
    def __init__(self):
        super().__init__()
        self.ninja_base_url = 'https://poe.ninja/api/data/'
        self.ninja_overviews = ['currencyoverview', 'itemoverview']
        self.ninja_currency_types = ['Currency', 'Fragment']
        self.ninja_item_types = ['Scarab', 'DivinationCard', 'Fragment', 'Fossil']

    def build_ninja_url(self, overview: str, item_type: str) -> str:
        query = '?league=%s&type=%s' % (self.trade_league, item_type)
        url = self.ninja_base_url + overview + query
        return url

    def get_ninja_api(self, overview: str, item_type: str) -> dict:
        url = self.build_ninja_url(overview, item_type)
        api_err_msg = f'- Can\'t access poe.ninja API'
        try:
            resp = requests.get(url)
            if resp.status_code == 200:
                return json.loads(resp.content)
            else:
                print(api_err_msg + f'\n  {url}')
        except Exception as e:
            self.log_error(e)

    def get_ninja_exalt_price(self) -> int:
        """Return poe ninja exalt price"""
        resp = self.get_ninja_api(self.ninja_overviews[0], self.ninja_currency_types[0])
        for obj in resp['lines']:
            if obj['currencyTypeName'] == 'Exalted Orb':
                return int(obj['chaosEquivalent'])

    def get_ninja_scarab_price(self, scarab_id: str) -> dict:
        """Return poe ninja singular scarab data"""
        resp = self.get_ninja_api(self.ninja_overviews[1], self.ninja_item_types[0])
        scarab = {}
        for obj in resp['lines']:
            if obj['detailsId'] == scarab_id:
                scarab.update({
                    'item_id': obj['detailsId'],
                    'item_listing_count': obj['listingCount'],
                    'chaos_value': obj['chaosValue'],
                    'price_change': obj['lowConfidenceSparkline'].get('totalChange', 0),
                })
                break
        return scarab


class ClientLog(Base):
    def __init__(self):
        Base.__init__(self)
        self.clientlog_path = self.app_config['TRADER']['client_log_path']

    def log_filter_by_time(self, line: str, time_limit=60) -> bool:
        """Filter log lines by time_limit"""
        try:
            time_now = datetime.now()
            match = re.search(
                r'(\d+(/|-)\d+(/|-)\d+\s\d+:\d+:\d+)',
                line).group().replace('/', '-')
            timestamp = datetime.strptime(match, self.date_fmt)
            time_passed = time_now - timestamp
            time_passed_sec = time_passed.total_seconds()
            return False if time_passed_sec > time_limit else True
        except Exception as e:
            self.log_error(e)
            return False

    def log_filter_datetime(self, line: str) -> tuple:
        """Return list of 2 elements: 0 - date(yyyy/mm/dd); 1 - time(hh:mm:ss)"""
        match = re.search(
            r'\d+/\d+/\d+\s\d+:\d+:\d+', line, flags=re.I)
        if match:
            span = match.span()
            datetime = tuple(line[span[0]:span[1]].split(' '))
            return datetime

    def log_filter_name(self, line: str, msg_type='from') -> str:
        """Return filtered character name from log string"""
        match = re.search(
            r'\@{}\s.+\:'.format(msg_type), line, flags=re.I)
        if match:
            span = match.span()
            char_name = line[span[0] + 5:span[1] - 1].strip()
            if ' ' in char_name:
                char_name = char_name.split(' ')[1]
            return char_name.strip()

    def log_filter_buy_msg(self, line: str) -> tuple:
        """Filter log line trade buy message - return tuple of 4"""
        msg = line.split(':')[3].strip()
        """Example: buy your 20 Polished Harbinger Scarab for"""
        msg_buy_item = re.search(r'(?<=your).*?(?=for)', msg, flags=re.I)
        """Example: my 98 Chaos Orb in"""
        msg_buy_currency = re.search(r'(?<=my).*?(?=in)', msg, flags=re.I)
        if not msg_buy_item or not msg_buy_currency:
            return None
        msg_buy_item = msg_buy_item[0].strip()
        msg_buy_currency = msg_buy_currency[0].strip()
        item_id = '-'.join(re.findall('([A-Za-z]+)', msg_buy_item.lower()))
        item_amount = int(re.search(r'\d+', msg_buy_item)[0])
        item_currency_id = '-'.join(re.findall('([A-Za-z]+)', msg_buy_currency.lower()))
        item_currency_amount = int(re.search(r'\d+', msg_buy_currency)[0])
        return ('buy', item_id, item_amount, item_currency_id, item_currency_amount)

    def log_filter_trade_error(self, line: str, msg_type='error') -> tuple:
        """Return filtered log error"""
        msg = line.split(':')[3].strip().lower()
        datetime = self.log_filter_datetime(line)
        if not datetime:
            return None
        return (msg_type, msg, datetime)

    def log_filter_instance_state(self, line: str) -> tuple:
        """Filter log line if player joined or left instance(hideout)"""
        msg_type = 'joined' if 'has joined' in line else 'left'
        char_name = line.split(':')[3].split('has')[0].strip()
        if '>' in char_name:
            char_name = char_name.split('>')[1].strip()
        datetime = self.log_filter_datetime(line)
        if not datetime:
            return None
        return (msg_type, char_name, datetime)

    def log_filter_trade_state(self, line: str) -> tuple:
        """Return filtered state for trade msg"""
        msg_type = 'accepted' if 'accepted' in line else 'cancelled'
        datetime = self.log_filter_datetime(line)
        if not datetime:
            return None
        return (msg_type, datetime)

    def log_build_buy_msg(self, line: str) -> tuple:
        """Return fully sliced and formated buy msg data"""
        char_name = self.log_filter_name(line, msg_type='from')
        datetime = self.log_filter_datetime(line)
        msg_data = self.log_filter_buy_msg(line)
        if not char_name or not datetime or not msg_data:
            return None
        return (char_name, msg_data, datetime)

    def log_manage(self, time_limit=60):
        """TODO: unit-test"""
        result = []
        with FileReadBackwards(self.clientlog_path, encoding="utf-8") as frb:
            for i, line in enumerate(frb):
                log_res = None
                if 'INFO' in line:
                    # lower line if not contain @from
                    if not self.log_filter_by_time(line, time_limit=time_limit):
                        break
                    if re.search(r'\@from', line, flags=re.I):
                        log_res = self.log_build_buy_msg(line)
                    elif re.search(r'has (joined|left)', line, flags=re.I):
                        log_res = self.log_filter_instance_state(line)
                    elif re.search(r'trade (accepted|cancelled)', line, flags=re.I):
                        log_res = self.log_filter_trade_state(line)
                    elif re.search(r'failed to join', line, flags=re.I):
                        log_res = self.log_filter_trade_error(line, msg_type='error')
                    elif re.search(r'go to this area from here', line, flags=re.I):
                        log_res = self.log_filter_trade_error(line, msg_type='area_error')
                if log_res:
                    result.append(log_res)
        return result


class Trader(TradeDB, Base):
    def __init__(self):
        Base.__init__(self)
        TradeDB.__init__(self)
        self.trader_config = self.app_config['TRADER']
        self.trader_switch = 0
        self.trade_api_url = self.trader_config['trade_api_url']
        self.trade_items_file = self.trader_config['trade_items_file']
        self.trade_single_template = self.trader_config['trade_single_tmplt']
        self.trade_bulk_template = self.trader_config['trade_bulk_tmplt']
        self.trade_bulk_types = json.loads(self.trader_config['trade_bulk_types'])
        self.max_bulk_price = int(self.trader_config['max_bulk_price'])
        self.max_stack_size = int(self.trader_config['max_stack_size'])
        self.fill_currency_stack = int(
            self.trader_config['fill_currency_stack'])
        self.priority_sleep = float(self.trader_config['priority_sleep'][1:-1])
        self.no_spam_delay = int(self.trader_config['no_spam_delay'])
        self.deduct_user_delay = int(self.trader_config['deduct_user_delay'])
        self.trade_ignored_users = []
        self.proxies = self.load_proxies()
        self.proxy = {}

    def load_trade_template(self, trade_item, bulk=False):
        trade_template = self.trade_bulk_template if bulk \
            else self.trade_single_template
        env = Environment(
            loader=PackageLoader('templates', 'trader'),
            autoescape=select_autoescape())
        template = env.get_template(trade_template)
        if bulk:
            template_rendered = template.render(
                have_item=[trade_item['buyout_currency']],
                want_item=[trade_item['item_id']],
                min_stock_amount=trade_item['min_stock_amount']
            ).replace("'", '"')
        else:
            template_rendered = template.render(
                item_name=trade_item['item_id'],
                item_name_type=trade_item['type'],
                price_min=trade_item['min_price'],
                price_max=trade_item['max_price']
            )
        return json.loads(template_rendered)

    def load_proxies(self, filename='proxies.txt'):
        try:
            proxies = []
            with open(filename, 'r') as file:
                proxies_raw = file.readlines()
                for line in proxies_raw:
                    proxies.append(line.strip().split(':'))
            return proxies
        except FileNotFoundError:
            with open(filename, 'w+') as file:
                file.write('')

    def proxy_rotate(self, protocol='http') -> dict:
        proxy = random.choice(self.proxies)
        if len(proxy) == 2:
            proxy = {
                protocol: '{0}://{1}:{2}'.format(
                    protocol, proxy[0], proxy[1]),
                'https': '{0}://{1}:{2}'.format(
                    protocol, proxy[0], proxy[1]),
            }
        else:
            proxy = {
                protocol: '{0}://{1}:{2}@{3}:{4}'.format(
                    protocol, proxy[2], proxy[3], proxy[0], proxy[1]),
                'https': '{0}://{1}:{2}@{3}:{4}'.format(
                    protocol, proxy[2], proxy[3], proxy[0], proxy[1])
            }
        print('- Proxy: ', proxy[protocol])
        return proxy

    def api_request(self, trade_item: dict, bulk=False) -> dict:
        print('- Requesting Trade API')
        template = self.load_trade_template(trade_item, bulk=bulk)
        bulk_url = f'{self.trade_api_url}/exchange/{self.trade_league}'
        nobulk_url = f'{self.trade_api_url}/search/{self.trade_league}'
        url = bulk_url if bulk else nobulk_url
        self.proxy = self.proxy_rotate()
        scraper = cloudscraper.create_scraper()
        resp = scraper.post(url, json=template, proxies=self.proxy, timeout=15)
        return json.loads(resp.content)

    def api_response_old(self, resp, trade_item, bulk=False):
        """Paginate initial POST response"""
        resp = json.loads(resp.content)
        resp_id = resp['id']
        resp_result = resp['result']
        resp_pagin_step = int(
            self.trader_config['trade_api_pagin_step_bulk'] if bulk
            else self.trader_config['trade_api_pagin_step'])
        resp_pagin = [  # list comprehension
            resp_result[i:i + resp_pagin_step]
            for i in range(0, len(resp_result), resp_pagin_step)]
        return (resp_id, resp_pagin)

    def api_fetch_page_old(self, resp_id, page_ids, bulk=False, cf=True):
        """Fetch page items with resp_id and page_ids"""
        page = ','.join(page_ids)
        fetch_url = f"{self.trade_api_url}/fetch/{page}"
        param_key = 'exchange' if bulk else 'query'
        self.proxy_rotate()
        if cf:
            scraper = cloudscraper.create_scraper()
            resp = scraper.get(
                fetch_url, params={param_key: resp_id},
                proxies=self.proxy, timeout=5)
        else:
            resp = requests.get(
                fetch_url,
                params={param_key: resp_id}
            )
        return resp

    def api_fetch_pages_old(self, resp, trade_item, bulk=False, delay=0.15):
        """
        Fetch pages item data and build/filter it
        resp_pagin = self.api_response(
            resp,
            trade_item,
            bulk=is_bulk)
        cleaned_data = self.api_fetch_pages(
            resp_pagin,
            trade_item,
            bulk=is_bulk)
        """
        resp_id, resp_pagin = resp
        cleaned_data = []

        for page_ids in resp_pagin:
            if not self.trader_switch:  # return cut of cleaned_data
                break
            try:
                page_resp = self.api_fetch_page(resp_id, page_ids, bulk=bulk)
            except Exception as e:
                print('- Error: ', repr(e))
                continue
            new_data, max_price_limit = self.build_cleaned_data(
                json.loads(page_resp.content),
                trade_item['buyout_currency'],
                bulk_data=bulk,
                max_price=trade_item['max_price'],
                max_stock_price=trade_item['max_stock_price'],
                min_stock_amount=trade_item['min_stock_amount'],
            )
            cleaned_data += new_data
            if delay:
                time.sleep(delay)
            if max_price_limit:  # break if max_price reached
                break

        if not bulk:
            self.update_nobulk_data_stack_size(cleaned_data)
        cleaned_data = self.sort_data_by_key(
            cleaned_data, 'item_stack_size')

        return cleaned_data

    def update_nobulk_data_stack_size(self, data):
        """
        TODO: Testing
        """
        acc_names = [obj['account_name'] for obj in data]
        acc_names_count = collections.Counter(acc_names)
        data = list({obj['account_name']: obj for obj in data}.values())

        for obj in data:
            acc_name_count = acc_names_count[data['account_name']]
            if data['item_stack_size'] and data['item_stack_size'] >= 2:
                data['item_stack_size'] += acc_name_count - 1
            else:
                data['item_stack_size'] = acc_name_count
        print(data)
        return data

    def build_cleaned_data_old(
            self, data, currency,
            bulk_data=False, max_price=None,
            max_stock_price=0, min_stock_amount=1):
        print('- Building cleaned data')
        max_price_limit = False
        cleaned_data = []
        for obj in data['result']:
            if not obj:
                continue
            listing = obj['listing']
            item = obj['item']
            account_name = listing['account']['name']
            account_ignored = [
                user for user in self.trade_ignored_users
                if account_name.lower() in user]
            if account_ignored:
                # print(f'- Ignored {account_name}')
                continue
            account_last_char_name = listing['account']['lastCharacterName']
            account_online = listing['account']['online']
            item_name = item['name']
            item_type_line = item['typeLine']
            item_art_filename = item.get('artFilename', None)
            item_flavour_text = item.get('flavourText', None)
            item_indexed = listing['indexed']
            whisper = listing['whisper']

            if bulk_data:
                item_price_amount = listing['price']['exchange']['amount']
                item_price_currency = listing['price']['exchange']['currency']
                item_id = listing['price']['item']['currency']
                item_stack_size = listing['price']['item']['stock']
                if 'fossil' in item_id and item_stack_size > 20:
                    item_stack_size = 20
            else:
                item_price_amount = listing['price']['amount']
                item_price_currency = listing['price']['currency']
                item_stack_size = item.get('stackSize', None)
                item_id = ''

            if item_id == 'simulacrum-splinter' and item_stack_size % 2:
                item_stack_size -= 1

            if bulk_data:
                bulk_item_amount = listing['price']['item']['amount']
                if bulk_item_amount > 1:  # if sold in proportions
                    item_price_amount = item_price_amount / bulk_item_amount
                bulk_price = round(item_stack_size * item_price_amount, 1)
                if 'scarab' in item_id:
                    if item_stack_size > self.max_stack_size:
                        item_stack_size = self.max_stack_size
                        bulk_price = round(
                            item_stack_size * item_price_amount, 1)
                    if bulk_price > self.max_bulk_price:
                        item_stack_size = math.floor(
                            self.max_bulk_price / item_price_amount)
                        bulk_price = round(
                            item_stack_size * item_price_amount, 1)
                whisper = whisper.format(item_stack_size, int(bulk_price))

            if item_stack_size < min_stock_amount:
                continue  # filter min_stock_amount

            if max_stock_price:  # filter max_stock_price
                if item_price_amount > max_stock_price:
                    max_price_limit = True
                    break
            else:  # filter max_price
                if item_price_amount > max_price:
                    max_price_limit = True
                    break

            if item_price_currency == currency:
                cleaned_data.append({
                    'account_name': account_name,
                    'account_last_char_name': account_last_char_name,
                    'account_online': account_online,
                    'item_price_amount': item_price_amount,
                    'item_price_currency': item_price_currency,
                    'item_stack_size': item_stack_size,
                    'item_name': item_name,
                    'item_type_line': item_type_line,
                    'item_art_filename': item_art_filename,
                    'item_flavour_text': item_flavour_text,
                    'item_indexed': item_indexed,
                    'whisper': whisper,
                })

        return (cleaned_data, max_price_limit)

    def sort_data_by_key(self, data, key, reverse=True):
        sorted_data = sorted(
            data,
            key=itemgetter(key),
            reverse=reverse
        )
        return sorted_data

    def send_whisper(self, whisper: str) -> None:
        self.app_window_focus()
        if self.check_app_window():
            self.pyperclip_copy(whisper)
            self.keyboard_ctrl_enter()
            self.keyboard_paste()
            self.keyboard_enter()

    def check_account_ignored(self, account_name):
        for user in self.trade_ignored_users:
            if account_name.lower() == user[1]:
                # print(f'- Ignored {account_name}')
                return True

    def calc_bulk_price(self, item_price: int, item_stock: int) -> tuple:
        """Calculate max_bulk_price and item_stock amount"""
        bulk_price = item_price * item_stock
        if bulk_price > self.max_bulk_price:
            item_stock = math.floor(self.max_bulk_price / item_price)
            if (item_stock / 5) >= 4:  # round stock to 5
                item_stock = math.floor(item_stock / 5) * 5
            bulk_price = item_price * item_stock
        return (round(bulk_price), item_stock)

    def build_cleaned_data(self, data: dict, trade_item: dict) -> list:
        """Clean/filter response for bulk data"""
        cleaned_data = []
        for key in data['result'].keys():  # result has list of trade_id objects
            obj = data['result'][key]['listing']
            account_name = obj['account']['name']
            account_last_char_name = obj['account']['lastCharacterName']
            account_online = obj['account']['online']
            item_buy_price = obj['offers'][0]['exchange']['amount']
            item_buy_currency = obj['offers'][0]['exchange']['currency']
            item_sell_id = obj['offers'][0]['item']['currency']
            item_sell_name = ' '.join([i.capitalize() for i in item_sell_id.split('-')])
            item_sell_amount = obj['offers'][0]['item']['amount']
            item_sell_stock = obj['offers'][0]['item']['stock']
            item_indexed = obj['indexed']
            whisper = obj['whisper'].format(
                obj['offers'][0]['item']['whisper'],
                obj['offers'][0]['exchange']['whisper'].replace('{0}', '{1}'))

            """data logic/manipulations here"""
            if self.check_account_ignored(account_name):
                continue
            if 'fossil' in item_sell_id and item_sell_stock > 20:  # test/remove this logic
                """limit fossils to 20 per trade"""
                item_sell_stock = 20
            if item_sell_amount > 1:
                """If item price listed in proportions"""
                item_buy_price = item_buy_price / item_sell_amount
            if item_buy_price > trade_item['max_stock_price']:
                """If max_price reached - skip"""
                continue

            bulk_price, item_sell_stock = self.calc_bulk_price(item_buy_price, item_sell_stock)
            whisper = whisper.format(item_sell_stock, bulk_price)

            cleaned_data.append({
                'account_name': account_name,
                'account_last_char_name': account_last_char_name,
                'account_online': account_online,
                'item_buy_price': item_buy_price,
                'item_buy_currency': item_buy_currency,
                'item_sell_id': item_sell_id,
                'item_sell_name': item_sell_name,
                'item_sell_amount': item_sell_amount,
                'item_sell_stock': item_sell_stock,
                'item_indexed': item_indexed,
                'whisper': whisper,
            })
        return cleaned_data

    def smart_whispers(self, db_conn, data: list, trade_item: dict) -> None:
        for obj in data:
            if not self.trader_switch:
                """If trader_switch was set False during operation, save/update queue result"""
                print(f'- Trader stopped in smart_whispers')
                now = datetime.now()
                while True:
                    if self.trader_switch:
                        print('- Trader Continue in smart_whispers')
                        break
                    time.sleep(0.2)
                passed = self.get_datetime_passed_seconds(
                    datetime.now(), time_now=now, reverse=True)
                if passed >= 60:
                    with self.whisper_queue.mutex:  # thread safe operation
                        self.whisper_queue.queue.clear()
                    break

            if not obj['account_online'] or obj['account_online'].get('status', None):
                """Skip afk and unknown users"""
                continue

            current_trade_user = self.db_get_object(
                db_conn, 'trade_users', 'acc_name', obj['account_name'])

            if current_trade_user:
                """Check last_trade_request - prevent spam"""
                last_trade_sec = self.get_datetime_passed_seconds(current_trade_user[-3])
                if last_trade_sec > 0 and last_trade_sec < self.no_spam_delay:
                    # print('- Skipped %s : %s' % (
                    #     obj['account_name'], obj['account_last_char_name']))
                    continue
                """Update current_trade_user data"""
                trade_user = (
                    obj['account_last_char_name'],
                    trade_item['type'],
                    obj['item_sell_id'],
                    obj['item_sell_name'],
                    obj['item_buy_price'],
                    obj['item_sell_stock'],
                    obj['item_buy_currency'],
                    obj['account_name'],
                )
                self.db_update_object(db_conn, self.sql_update_trade_user, trade_user)
            else:
                trade_user = (
                    obj['account_name'],
                    obj['account_last_char_name'],
                    trade_item['type'],
                    obj['item_sell_id'],
                    obj['item_sell_name'],
                    obj['item_buy_price'],
                    obj['item_sell_stock'],
                    obj['item_buy_currency'],
                    str(datetime.now()),
                )
                self.db_create_object(db_conn, self.sql_insert_trade_user, trade_user)
                current_trade_user = self.db_get_object(
                    db_conn, 'trade_users', 'acc_name', obj['account_name'])
                if not current_trade_user:
                    continue
            self.whisper_queue.put((current_trade_user, obj['whisper']))
            time.sleep(current_trade_user[-2] / self.priority_sleep)  # 10 / 1.n

    def manage_trade_whisper_queue(self):
        db_conn = self.db_create_connection()
        while True:
            if self.whisper_queue.empty() or not self.trader_switch:
                time.sleep(0.2)
                continue
            current_trade_user, whisper = self.whisper_queue.get()
            print("- Sent whisper to %s : %s" % (current_trade_user[1], current_trade_user[2]))
            self.send_whisper(whisper)
            self.db_update_trade_user_priority(db_conn, current_trade_user, str(datetime.now()))
            time.sleep(3)
            self.whisper_queue.task_done()

    def run_trader(self, trade_items_file):
        db_conn = self.db_create_connection()
        # create project db tables
        self.db_create_tables(db_conn)
        # add default ignored_users
        self.trade_ignored_users = self.db_insert_default_ignored_users(db_conn)
        # check for new ignored_users in file
        self.db_insert_new_ignored_users(db_conn)
        trade_items_file = 'temp/' + trade_items_file
        trade_items = self.load_json_file(trade_items_file)
        trade_items_len = len(trade_items)
        trade_item_counter = 0

        while True:
            if self.trader_switch:
                new_trade_items = self.load_json_file(trade_items_file)
                if trade_items != new_trade_items:  # update trade_items
                    trade_items = new_trade_items

                trade_item = trade_items[trade_item_counter]
                print(f'\n- Switched to {trade_item["item_id"]}')

                """Check if trade_item buy_limit reached"""
                try:
                    trade_summary = self.load_json_file(self.trade_summary_path)
                except Exception as e:
                    print('- Error:', e)
                    trade_summary = []

                if trade_summary:
                    buy_limit = False
                    for summary in trade_summary:
                        """check if trade_summary amount == buy_limit"""
                        if trade_item['item_id'] == summary['item_id']:
                            """update trade_summary item_buy_price"""
                            if not summary.get('item_buy_price'):
                                summary.update({'item_buy_price': trade_item['max_stock_price']})
                                self.update_json_file(trade_summary, self.trade_summary_path)
                                print('- Trade summary updated')
                            """Set trade_item buy_limit"""
                            if summary['item_amount'] >= trade_item['buy_limit']:
                                buy_limit = True
                                break
                    if buy_limit:
                        print('- Buy limit reached:', trade_item['item_id'], trade_item['buy_limit'])
                        trade_item['max_stock_price'] -= 1
                        trade_item['min_stock_amount'] = 2
                        trade_item['buy_limit'] += 100
                        buy_limit = False

                """Check if trade_item disabled"""
                if trade_item['disabled']:
                    if trade_item_counter >= trade_items_len - 1:
                        trade_item_counter = 0
                    else:
                        trade_item_counter += 1
                    print('  Disabled')
                    continue

                try:
                    is_bulk = bool(trade_item['type'] in self.trade_bulk_types)
                    response = self.api_request(trade_item, bulk=is_bulk)
                    response = self.build_cleaned_data(response, trade_item)
                    with db_conn:
                        self.smart_whispers(db_conn, response, trade_item)
                    time.sleep(4)
                except Exception as e:
                    sleep_duration = 30
                    api_err_msg = "Can't access Trade API\n"
                    api_overuse_msg = api_err_msg + f'API overuse - Sleep {sleep_duration}s'
                    if 'result' in repr(e) or 'id' in repr(e) or '429' in repr(e):
                        # self.log_error(e)
                        print(repr(e))
                        print(api_overuse_msg)
                        # self.show_toast(api_overuse_msg, msg_type='INFO')
                        time.sleep(sleep_duration)
                    else:
                        # self.log_error(e)
                        print(e)
                        print(f'- {repr(e)}', f'\n  PROXY: {self.proxy}',)
                        # self.show_toast(api_err_msg + str(e))
                    print('\n- Trader Restart')
                    continue

                if trade_item_counter >= trade_items_len - 1:
                    trade_item_counter = 0
                else:
                    trade_item_counter += 1
                time.sleep(1)
            time.sleep(self.main_loop_delay)
        return 0


class TradeBot(Prices, ClientLog, Trader, KeyActions, OCRChecker):
    def __init__(self):
        self.whisper_queue = Queue()
        Prices.__init__(self)
        ClientLog.__init__(self)
        Trader.__init__(self)
        KeyActions.__init__(self)
        OCRChecker.__init__(self)
        self.prices = []
        self.STATE = None
        self.STATES = {
            'START': 'START',
            'HIDEOUT': 'HIDEOUT',
            'PRETRADE': 'PRETRADE',
            'TRADE': 'TRADE',
        }
        self.hideout_state = []
        self.trade_timer_limit = 150
        self.stash_items_position = {
            'bestiary': [85, 210],
            'reliquary': [85, 275],
            'torment': [85, 340],
            'sulphite': [85, 405],
            'metamorph': [85, 470],
            'legion': [85, 535],
            'ambush': [85, 600],
            'blight': [85, 665],
            # Second column
            'shaper': [370, 210],
            'expedition': [370, 275],
            'cartography': [370, 340],
            'harbinger': [370, 405],
            'elder': [370, 470],
            'divination': [370, 535],
            'breach': [370, 600],
            'abyss': [370, 665],
        }

    def set_state(self, status):
        self.STATE = status
        print(f'\n- State changed: {status}')

    def update_trade_summary(self, trade_item_id: str, amount: int, decr=False) -> None:
        """Create trade_summary if not exist; create summary template/update item_amount"""
        if not os.path.exists(self.trade_summary_path):
            with open(self.trade_summary_path, 'w', encoding='utf-8') as f:
                f.write('{}')
        summary_template = {
            'item_id': trade_item_id,
            'item_amount': 0,
            'item_buy_price': 0,
            'item_sell_price': 0
        }
        data = self.load_json_file(self.trade_summary_path)
        if not data:
            data = []
        """Update trade_item_amount"""
        for trade_item in data:
            if trade_item['item_id'] == trade_item_id:
                if decr:
                    trade_item['item_amount'] -= int(amount)
                else:
                    trade_item['item_amount'] += int(amount)
                self.update_json_file(data, self.trade_summary_path)
                print('- Trade summary updated')
                return
        """Add new summary_template"""
        summary_template['item_amount'] += int(amount)
        data.append(summary_template)
        self.update_json_file(data, self.trade_summary_path)

    def stash_activate_tab(self, tab: str, subtab='') -> None:
        """Activate one of the stash tabs if stash is opened"""
        tabs = {
            "currency": {
                "main": (75, 110),
                "sub": {
                    "general": (250, 145),
                    "exotic": (425, 145),
                },
            },
            "fragment": {
                "main": (160, 110),
                "sub": {
                    "general": (95, 145),
                    "breach": (250, 145),
                    "scarab": (410, 145),
                },
            },
        }
        try:
            print('- Activating tab:', tab)
            tab_main = tabs[tab]['main']
            tab_sub = tabs[tab]['sub'].get(subtab, None)
            self.mouse_move(*tab_main)
            time.sleep(0.2)
            pyautogui.click()
            if tab_sub:
                self.mouse_move(*tab_sub)
                time.sleep(0.2)
                pyautogui.click()
        except KeyError:
            print('- Error! Incorrect tab/subtab name:', tab, subtab)

    def stash_get_scarab_position(self, item_id: str) -> list:
        """Scarab tab has 2 columns; there 16 types; each scarab has 4 tiers
           Column element split equals to 70px
           Calculate xy row position for 4 tiers
           Choose which item_id[prefix == scarab_tier
        """
        scarab_tiers = ['rusted', 'polished', 'gilded', 'winged']
        scarab_id = item_id.split('-')
        scarab_pos = self.stash_items_position[scarab_id[1]]
        x_row = [i * 70 for i in range(4)]  # generate scarab tier x positions
        rows = [(i + scarab_pos[0], scarab_pos[1]) for i in x_row]
        for i, tier in enumerate(scarab_tiers):
            if tier == scarab_id[0]:  # scarab_id prefix
                return rows[i]

    def stash_take_item(self, item_id: str, amount=0) -> None:
        print('- Taking item {} - {}'.format(item_id, amount))
        if 'scarab' in item_id:
            self.stash_activate_tab('fragment', subtab='scarab')
            amount = math.ceil(amount / 10)  # calc amount of clicks ;+1 for safety
            scarab_pos = self.stash_get_scarab_position(item_id)
            self.mouse_move(*scarab_pos)
            time.sleep(0.3)
            self.mouse_move_click(clicks=amount, interval=0.25, ctrl=True)

    def stash_set_item_price(self, item_id: str, price: str) -> None:
        print('- Setting price {} for {}'.format(price, item_id))
        if 'scarab' in item_id:
            self.stash_activate_tab('fragment', 'scarab')
            time.sleep(0.3)
            """Activate sell dropdown"""
            scarab_pos = self.stash_get_scarab_position(item_id)
            self.mouse_move(*scarab_pos)
            time.sleep(0.2)
            self.mouse_move_click(btn='right')
            """Hover over price dropdown"""
            for i in range(3):  # attemp to get dropdown btn
                x_pos, y_pos = self.check_stash_item_dropdown()
                if x_pos:
                    break
            self.mouse_move(x_pos, y_pos, delay=True)
            self.mouse_move_click()
            """Select dropdown type - Exact Price"""
            x_pos, y_pos = pyautogui.position()
            self.mouse_move(x_pos, y_pos + 80, delay=True)
            self.mouse_move_click()
            """Hover over price box - click"""
            x_pos, y_pos = pyautogui.position()
            self.mouse_move(x_pos + 55, y_pos - 80, delay=True)
            self.mouse_move_click()
            """Insert price"""
            self.keyboard_select_text()
            time.sleep(0.2)
            self.pyperclip_copy(price)
            self.keyboard_paste()
            """Click accept button"""
            x_pos, y_pos = pyautogui.position()
            self.mouse_move(x_pos + 240, y_pos + 45, delay=True)
            self.mouse_move_click()

    def check_item(self, item_name, amount=0, trade='', inventory=False):
        if inventory:
            crop = self.crop['inventory']
        elif trade:
            crop = self.crop['trade_{}'.format(trade)]  # all/top/bottom
        else:
            crop = []

        if 'chaos' in item_name:
            threshold = {
                1: 0.9,
                2: 0.9,
                3: 0.9,
                4: 0.9,
                5: 0.9,
                6: 0.9,
                7: 0.9,
                8: 0.89,
                9: 0.89,
                10: 0.88,
            }
            template = f'assets/items/c_chaos_{amount}_{amount}.png'
            threshold = threshold[amount]
        elif 'exalt' in item_name:
            template = f'assets/items/exalt-half.png'
            threshold = 0.85
        elif item_name == 'card':
            template = 'assets/items/card_half.png'
            threshold = 0.8
        elif 'fossil' in item_name:
            template = f'assets/items/{item_name}.png'
            threshold = {
                'bound-fossil': 0.85,
                'corroded-fossil': 0.85,
                'perfect-fossil': 0.8,
                'prismatic-fossil': 0.8,
                'shuddering-fossil': 0.85,
                'sanctified-fossil': 0.82,
            }
            threshold = threshold[item_name]
        else:
            """TODO: Test rusted meta/harbinger/perfect-fossil"""
            template = f'assets/items/{item_name}.png'
            threshold = {
                'rusted-ambush-scarab': 0.74,
                'polished-ambush-scarab': 0.76,
                'gilded-ambush-scarab': 0.84,
                'gilded-abyss-scarab': 0.81,
                'rusted-bestiary-scarab': 0.74,
                'polished-bestiary-scarab': 0.79,
                'rusted-blight-scarab': 0.66,
                'gilded-blight-scarab': 0.75,
                'gilded-bestiary-scarab': 0.81,
                'rusted-divination-scarab': 0.7,
                'polished-divination-scarab': 0.81,
                'gilded-divination-scarab': 0.81,
                'rusted-expedition-scarab': 0.7,
                'polished-expedition-scarab': 0.8,
                'rusted-sulphite-scarab': 0.74,
                'polished-sulphite-scarab': 0.71,
                'gilded-sulphite-scarab': 0.82,
                'rusted-metamorph-scarab': 0.71,
                'polished-metamorph-scarab': 0.75,
                'gilded-metamorph-scarab': 0.79,
                'rusted-legion-scarab': 0.78,
                'polished-legion-scarab': 0.8,
                'polished-breach-scarab': 0.8,
                'gilded-breach-scarab': 0.84,
                'rusted-harbinger-scarab': 0.70,
                'polished-harbinger-scarab': 0.8,
                'gilded-harbinger-scarab': 0.81,
                'polished-cartography-scarab': 0.75,
                'gilded-cartography-scarab': 0.8,
            }
            threshold = threshold[item_name]
        detected_objects = self.cv_detect_boilerplate(
            template, threshold=threshold,
            lst=True, calc_mp=True, crop=crop)[0]
        if detected_objects:
            multiple = True if len(detected_objects) >= 2 else False
            detected_objects = self.double_check_item(
                detected_objects, item_name, amount=amount, crop=crop, multiple=multiple)
            if 'exalt' in item_name:  # exalt check returns int
                return detected_objects
        filtered_objects = list()
        for pt in detected_objects:
            filtered_objects.append((pt[0], pt[1], amount))
        return sorted(filtered_objects)

    def double_check_item(self, items, item_name, amount=0, crop=[], multiple=False):
        detected_objects = []
        if 'exalt' in item_name:
            template = 'assets/items/exalt-{}.png'
            threshold = {
                1: 0.9,
                2: 0.9,
                3: 0.9,
                4: 0.9,
                5: 0.9,
                6: 0.9,
                7: 0.9,
                8: 0.9,
                9: 0.9,
                10: 0.9,
            }
            item_sum = 0
            for item in items:
                # crop item upper half number for checking
                crop = [item[0] - 30, item[1] - 35, item[0] + 25, item[1]]
                for i in threshold:
                    detected_objects = self.cv_detect_boilerplate(
                        template.format(i), threshold=threshold[i],
                        lst=True, crop=crop)[0]
                    if detected_objects:
                        item_sum += i
                        break
            return item_sum
        elif 'fossil' in item_name:
            template = f'assets/items/fossil-{amount}.png'
            threshold = {
                1: 0.86,
                2: 0.86,
                3: 0.86,
                4: 0.86,
                5: 0.86,
                6: 0.86,
                7: 0.86,
                8: 0.86,
                9: 0.86,
                10: 0.82,
                11: 0.8,
                12: 0.8,
                13: 0.8,
                14: 0.8,
                15: 0.8,
                16: 0.8,
                17: 0.8,
                18: 0.8,
                19: 0.8,
                20: 0.8,
            }
            if multiple:
                item_sum = 0
                for i in range(1, len(threshold) + 1):
                    if item_sum >= amount:
                        print('- Item sum:', item_sum)
                        break
                    template = f'assets/items/fossil-{i}.png'
                    detected_objects = self.cv_detect_boilerplate(
                        template, threshold=threshold[i],
                        lst=True, crop=crop)[0]
                    for ii in detected_objects:
                        item_sum += i
                return items if item_sum >= amount else []
            else:
                threshold = threshold[amount]
                detected_objects = self.cv_detect_boilerplate(
                    template, threshold=threshold,
                    lst=True, crop=crop)[0]
                return items if detected_objects else []
        elif 'scarab' in item_name:
            if amount > 10:
                multiple = True
            template = f'assets/items/scarab-{amount}.png'
            threshold = {
                1: 0.82,
                2: 0.83,
                3: 0.82,
                4: 0.82,
                5: 0.82,
                6: 0.82,
                7: 0.82,
                8: 0.82,
                9: 0.82,
                10: 0.82,
            }
            if multiple:
                item_sum = 0
                for i in range(1, len(threshold) + 1):
                    if item_sum >= amount:
                        print('- Item sum:', item_sum)
                        break
                    template = f'assets/items/scarab-{i}.png'
                    detected_objects = self.cv_detect_boilerplate(
                        template, threshold=threshold[i],
                        lst=True, crop=crop)[0]
                    for ii in detected_objects:
                        item_sum += i
                return items if item_sum >= amount else []
            else:
                threshold = threshold[amount]
                detected_objects = self.cv_detect_boilerplate(
                    template, threshold=threshold,
                    lst=True, crop=crop)[0]
                return items if detected_objects else []
        elif 'card' in item_name:
            threshold = {
                1: 0.8,
                2: 0.8,
                3: 0.8,
                4: 0.8,
                5: 0.8,
                6: 0.8,
            }
            if multiple:
                item_sum = 0
                for i in range(1, len(threshold) + 1):
                    if item_sum >= amount:
                        print('- Item sum:', item_sum)
                        break
                    template = f'assets/items/card_{i}.png'
                    detected_objects = self.cv_detect_boilerplate(
                        template, threshold=threshold[i],
                        lst=True, crop=crop)[0]
                    for ii in detected_objects:
                        item_sum += i
                return items if item_sum >= amount else []
            else:
                if amount > 6:
                    amount = 6
                threshold = threshold[amount]
                template = f'assets/items/card_{amount}.png'
                detected_objects = self.cv_detect_boilerplate(
                    template, threshold=threshold,
                    lst=True, crop=crop)[0]
                return items if detected_objects else []
        return items

    def trade_confirm_items(self, coords, trade_user='', validate=False):
        result = list()
        trade_accepted = self.check_trade_accepted()
        if not self.check_trade_opened():
            return result
        for pt in sorted(coords, reverse=True):
            if not trade_accepted:
                self.mouse_move(pt[0], pt[1])
                time.sleep(0.05)
            if validate and trade_user:
                for i in range(0, 4):
                    item_valid = self.trade_validate_item(
                        trade_user[3],
                        trade_user[4])
                    if item_valid:
                        result.append(item_valid)
                        break
        self.mouse_move(550, 835)
        time.sleep(0.3)
        return result

    def trade_validate_item(self, item_type, item_id, threshold=0.8):
        item_id = item_id.replace('-', '_')
        template = f'assets/items/{item_type}_{item_id}.png'
        detected_objects = self.cv_detect_boilerplate(
            template, threshold=threshold)[0]
        return detected_objects[0] if detected_objects else None

    def fill_from_stash(self, threshold=0.85):
        template = 'assets/items/stash_chaos.png'
        detected_objects = self.cv_detect_boilerplate(
            template,
            threshold=threshold,
            calc_mp=True, lst=True, crop=self.crop['stash'])[0]

        if not detected_objects or not self.check_stash_opened():
            return None

        if len(detected_objects) >= 2:
            detected_objects = detected_objects[-1]
        else:
            detected_objects = detected_objects[0]

        x_mp = detected_objects[0]
        y_mp = detected_objects[1]
        fill_count = 0

        while True:
            surplus = self.check_remove_surplus(
                max_amount=self.fill_currency_stack)
            if fill_count >= 15:
                break
            if not surplus:
                print('- Filling inventory')
                self.mouse_move(x_mp, y_mp)
                self.mouse_move_click(x_mp, y_mp, clicks=1, ctrl=True)
                if self.check_stash_currency():
                    print('- No stash currency')
                    break
            else:
                print('- Inventory filled')
                self.mouse_move(1350, 500)
                self.check_remove_trails()
                # prevent currency cliping
                fill_count = 0
                empty_slots = self.check_empty_slot(inventory=True)
                if empty_slots:
                    self.mouse_move_click(
                        empty_slots[0][0], empty_slots[0][1],
                        interval=0.1)
                break
            fill_count += 1

    def unstuck_currency(self, coords, amount):
        empty_slots = self.check_empty_slot(inventory=True)
        if len(empty_slots) >= 2:
            self.mouse_move(coords[0], coords[1])
            pyautogui.keyUp('shift')
            pyautogui.keyDown('shift')
            pyautogui.click()
            pyautogui.press(str(amount))
            pyautogui.keyUp('shift')
            time.sleep(0.2)
            self.mouse_move(coords[0] + 95, coords[1] - 40)
            time.sleep(0.2)
            pyautogui.click()
            self.mouse_move(empty_slots[0][0], empty_slots[0][1], delay=True)
            del empty_slots[0]
            pyautogui.doubleClick()
            time.sleep(0.2)
            # prevent currency cliping
            self.mouse_move(empty_slots[0][0], empty_slots[0][1], delay=True)
            time.sleep(0.1)
            pyautogui.click()
            self.mouse_move(1350, 500)
        else:
            raise IndexError

    def game_invite(self, coords, accept=True):
        if accept:
            x_btn = int(coords[0] + 100)
            y_btn = int(coords[1] + 130)
        else:
            x_btn = int(coords[0] + 300)
            y_btn = int(coords[1] + 130)
        self.mouse_move(x_btn, y_btn)
        time.sleep(0.1)
        self.mouse_move_click(clicks=2, delay=False)

    def ocr_user_deduct(self, db_conn, ocr_text):
        user_amount = 15
        latest_trade_users = self.db_get_latest_objects(
            db_conn,
            'trade_users',
            'last_trade',
            amount=user_amount)
        deducted_user = None
        for trade_user in latest_trade_users:
            user_similarity = self.check_string_similarity(
                ocr_text, trade_user[1])
            if user_similarity >= 0.7:
                print('- Similarity:', ocr_text, trade_user[1], user_similarity)
                deducted_user = trade_user
                break
        return deducted_user

    def trade_user_deduct(self, db_conn, ocr_user=None):
        user_amount = 9
        latest_trade_users = self.db_get_latest_objects(
            db_conn,
            'trade_users',
            'last_trade',
            amount=user_amount)
        deducted_user = None
        for i, trade_user in enumerate(latest_trade_users):
            if ocr_user and i >= 2:
                ocr_user = None
                i = 0
            trade_user = ocr_user if ocr_user else trade_user
            if self.check_not_in_party() or not self.check_in_party() or deducted_user:
                break
            self.action_hideout_join(trade_user[2])
            if i == 0:
                self.action_hideout_join(trade_user[2])
            for i in range(self.deduct_user_delay):
                if self.check_loading():
                    print(f'- Found current_trade_user: {trade_user[2]}')
                    deducted_user = trade_user
                    break
        return deducted_user

    def prepare_currency(self, trade_user):
        currency_name = trade_user[8]
        currency_bulk = trade_user[6] * trade_user[7]
        if isinstance(currency_bulk, float) and currency_name == 'chaos':
            currency_bulk = math.ceil(currency_bulk)
        currency_calc = str(currency_bulk / 10).split('.')
        currency_stack = int(currency_calc[0])
        currency_amount = int(currency_calc[1])

        self.check_open_inventory()

        for i in range(7):
            detected_objects_stack = self.check_item(
                currency_name,
                amount=10,
                inventory=True)
            if not detected_objects_stack:
                self.check_open_inventory()
                time.sleep(1)
            else:
                break

        if not currency_amount:
            detected_objects = detected_objects_stack[:currency_stack]
            return detected_objects

        try:
            self.unstuck_currency(
                detected_objects_stack[0],
                currency_amount)
        except IndexError:
            print('- Unable to unstuck currency')
            self.action_command_chat(self.cmd_kick)
            self.action_command_chat(self.cmd_kick)
            self.set_state('START')
            return list()
        time.sleep(0.3)
        del detected_objects_stack[0]
        detected_objects = self.check_item(
            currency_name,
            amount=currency_amount,
            inventory=True)

        if detected_objects:
            if currency_stack:
                currency_coords = detected_objects_stack[:currency_stack]
                currency_coords.append(detected_objects[-1])
                return currency_coords
            else:
                return [detected_objects[-1]]

    def filter_trade_users_done(self, user_done: str, time_limit=60) -> str:
        """Filter user_done by time_limit;"""
        user_done_passed = datetime.now() - datetime.fromisoformat(user_done.split('%')[1])
        if user_done_passed.seconds >= time_limit:
            return None
        return user_done

    def manage_invites(self, trade=False, party=False):
        invites = self.check_invite(check_type=True)
        for invite in invites:
            if invite[2] == 'trade':
                if trade:
                    self.game_invite(invite, accept=True)
                    empty_slots = self.check_empty_slot(
                        inventory=True)
                    try:
                        self.mouse_move(
                            empty_slots[0][0], empty_slots[0][1])
                        time.sleep(0.1)
                        pyautogui.click()
                    except IndexError:
                        self.action_command_chat(self.cmd_kick)
                        self.set_state(None)
            elif invite[2] == 'party':
                if party:
                    self.game_invite(invite, accept=True)
                    return True
            elif invite[2] == 'friend':
                self.game_invite(invite, accept=True)
            elif invite[2] == 'challenge':
                self.game_invite(invite, accept=False)
            elif invite[2] == 'unknown':
                pass

    def manage_trade(self, give_items, trade_user):
        """TODO: Merge manage_trade_sell"""
        give_items_amount = list(dict.fromkeys([pt[2] for pt in give_items]))
        given_items = []
        for item_amount in give_items_amount:
            det_objects = self.check_item(trade_user[-4], amount=item_amount, trade='bottom')
            if det_objects:
                [given_items.append(i) for i in det_objects]
        given_items_len = len(given_items)
        give_items_len = len(give_items)
        print('- Given Items:', given_items_len, give_items_len)
        if given_items_len < give_items_len:
            if not self.check_trade_opened():
                return None
            pyautogui.click(button='right', interval=0.1)  # why?
            for item_coords in give_items:
                self.mouse_move(*item_coords)
                time.sleep(0.05)
                self.mouse_move_click(ctrl=True)
            self.mouse_move(1350, 500)
        elif given_items_len == give_items_len:
            trade_item = trade_user[3] if trade_user[3] == 'card' else trade_user[4]
            trade_user_items = self.check_item(
                trade_item,
                amount=trade_user[7],
                trade='top')
            if trade_user_items:
                validate = True if trade_user[3] == 'card' else False
                result = self.trade_confirm_items(
                    trade_user_items,
                    trade_user=trade_user,
                    validate=validate)
                if trade_user[3] == 'fossil' or trade_user[3] == 'scarab':
                    trade_user_items = list(range(0, trade_user[7]))
                if trade_user[3] == 'card':
                    if len(result) >= len(trade_user_items):
                        self.check_trade_opened(accept=True)
                    return None
                if len(trade_user_items) == len(result):
                    self.check_trade_opened(accept=True)
                print('- User items:', len(trade_user_items), trade_user[7])
                if len(trade_user_items) >= trade_user[7]:
                    self.check_trade_opened(accept=True)

    def manage_trade_sell(self, give_items: list, trade_user: tuple) -> None:
        """TODO: Compare give_items and trade_user amount - prevent overhead trade"""
        give_items_amount = list(dict.fromkeys([pt[2] for pt in give_items]))
        given_items = []
        for item_amount in give_items_amount:  # unique item_amounts
            det_objects = self.check_item(trade_user[1][1], amount=item_amount, trade='bottom')
            if det_objects:
                [given_items.append(i) for i in det_objects]
        given_items_len = len(given_items)
        give_items_len = len(give_items)
        print('- Given Items:', given_items_len, give_items_len)
        if given_items_len < give_items_len:  # check my items
            if not self.check_trade_opened():
                return
            for item_coords in give_items:
                self.mouse_move(*item_coords)
                time.sleep(0.05)
                self.mouse_move_click(ctrl=True)
            self.mouse_move(1350, 500)  # inventory to the left of flasks
        elif given_items_len == give_items_len:  # check trade_user items
            if not self.check_trade_accepted():
                self.action_confirm_items(delay=0.02)  # confirm items before checking
                self.mouse_move(605, 485)  # middle of the trade window

            exalt_price = self.prices[0]['chaos_value']
            take_currency_type = trade_user[1][3]
            take_currency_amount = 10 if trade_user[1][4] > 10 else trade_user[1][4]
            take_currency_amount_rem = trade_user[1][4] % 10
            take_items = self.check_item(
                take_currency_type,  # chaos-orb
                amount=take_currency_amount,
                trade='top')
            if take_currency_amount_rem:
                remainders = self.check_item(
                    take_currency_type,  # chaos-orb
                    amount=take_currency_amount_rem,
                    trade='top')
                [take_items.append(i) for i in remainders]
            # find / calculate exalt sum
            exalt_sum = self.check_item('exalted-orb', trade='top')
            exalt_sum = round(exalt_sum * exalt_price) if exalt_sum else 0
            # calculate user given currency amount
            item_sum = sum([i[2] for i in take_items]) + exalt_sum
            # check if sum is appropriate - accept trade
            if take_items or exalt_sum:
                print('- Item Sum:', item_sum, '- Exalt Sum:', exalt_sum)
                if item_sum >= trade_user[1][4]:
                    self.check_trade_opened(accept=True)

    def manage_hideout_state(self):
        prev_state = []
        while True:
            if self.check_hideout():
                log_result = self.log_manage(time_limit=5)
                if self.hideout_state != prev_state:
                    print('- Hideout State:', self.hideout_state)
                    prev_state = self.hideout_state
                for log in log_result:
                    log_type = log[0]
                    trade_user_name = log[1]
                    if log_type == 'joined':
                        if trade_user_name not in self.hideout_state:
                            print('- Joined:', trade_user_name)
                            self.hideout_state.append(trade_user_name)
                    elif log_type == 'left':
                        if trade_user_name in self.hideout_state:
                            print('- Left:', trade_user_name)
                            self.hideout_state.remove(trade_user_name)
            time.sleep(0.5)

    def manage_prices(self):
        while True:
            try:
                exalt_price = self.get_ninja_exalt_price()
                if exalt_price:
                    self.prices.clear()
                    self.prices.append({'item_id': 'exalted-orb', 'chaos_value': exalt_price})
                print('- Prices:', self.prices)
            except Exception as e:
                print('- Error manage_prices:', repr(e))
            time.sleep(300)

    def run_seller(self):
        trade_users = []
        trade_users_done = []
        trade_summary = self.load_json_file(self.trade_summary_path)

        while True:
            if not self.STATE:
                trade_users.clear()
                trade_opened = False
                trade_timer = 0
                current_trade_user = None
                inventory_items = []
                self.set_state('START')
                continue
            elif self.STATE == 'START':
                """Check trade_summary and set prices"""
                self.app_window_focus()

                # Open stash
                while not self.check_stash_opened():
                    self.check_open_stash()
                    time.sleep(1)
                # remove alerts + clean inventory
                self.check_remove_alerts()
                time.sleep(0.2)
                self.action_paste_inventory_all()
                self.mouse_move(1350, 500)  # inventory to the left of flasks
                # Set stash prices
                for summary in trade_summary:
                    if not summary['item_sell_price'] and summary['item_amount'] >= 50:
                        if 'scarab' not in summary['item_id']:
                            # TODO: remove later when more item_ids
                            continue
                        # item_price in proportion
                        item_price_incr = 1.5 if summary['item_buy_price'] >= 7 else 0.9
                        item_price = str(
                            round((summary['item_buy_price'] + item_price_incr) * 10)) + '/10'
                        self.stash_set_item_price(summary['item_id'], item_price)
                        summary['item_sell_price'] = item_price
                        self.update_json_file(trade_summary, self.trade_summary_path)
                trade_summary = self.load_json_file(self.trade_summary_path)
                self.set_state('HIDEOUT')
                continue
            elif self.STATE == 'HIDEOUT':
                """Check log - filter trade_users_done - invite user"""
                """TODO: party invite_limit"""
                # check if trade_user[name] in hideout_state
                if [user for user in trade_users if user[0] in self.hideout_state]:
                    self.set_state('PRETRADE')
                    continue
                # filter trade_users_done by time and remove old ones
                trade_users_done[:] = [
                    i for i in trade_users_done if self.filter_trade_users_done(i, time_limit=60)]
                # filter log buy messages and send party invite/sold
                log_result = self.log_manage(time_limit=50)
                for log in log_result:
                    if log[1][0] != 'buy':
                        continue
                    char_name, buy_item, timestamp = log
                    user_buy_price = round(buy_item[4] / buy_item[2], 1)
                    for summary in trade_summary:
                        if summary['item_id'] != buy_item[1]:  # compare id
                            continue
                        if user_buy_price < eval(str(summary['item_sell_price'])):
                            if char_name in trade_users_done:
                                continue
                            print('- User {} changed sell price'.format(char_name))
                            trade_users_done.append(char_name + '%' + str(datetime.now()))
                            continue

                        check_user_in_done = [i for i in trade_users_done if char_name in i]
                        if check_user_in_done:
                            continue

                        # Sold / Invite logic
                        if summary['item_amount'] < buy_item[2]:  # item sold
                            time.sleep(0.3)
                            self.action_command_chat(f'@{char_name} sold')
                            trade_users_done.append(char_name + '%' + str(datetime.now()))
                        elif summary['item_amount'] >= buy_item[2]:  # item available
                            # send party invite, save trade_user
                            check_user_in = [i for i in trade_users if i[0] == char_name]
                            if check_user_in:  # if user in trade_users - skip
                                """TODO: check if user updated item_amount"""
                                continue
                            self.app_window_focus()
                            time.sleep(1)
                            print('- Invited ', char_name)
                            self.action_command_chat(self.cmd_invite + char_name)
                            trade_users.append(log)
                time.sleep(0.5)
            elif self.STATE == 'PRETRADE':
                """Prepare inventory items"""
                current_trade_user = [i for i in trade_users if i[0] in self.hideout_state]
                if not current_trade_user:
                    print('- Unknown current_trade_user:', self.hideout_state, trade_users)
                    self.set_state('HIDEOUT')
                    continue
                current_trade_user = current_trade_user[0]
                item_id = current_trade_user[1][1]
                item_amount = current_trade_user[1][2]
                print('- Current user:', current_trade_user)
                # Open stash
                while not self.check_stash_opened():
                    self.check_open_stash()
                    time.sleep(1)
                # Take trade items from stash / confirm result
                inventory_items = self.check_item(item_id, amount=item_amount, inventory=True)
                if inventory_items:
                    print('- Inventory items len:', len(inventory_items))
                    self.set_state('TRADE')
                    continue
                else:
                    """TODO: bug stash_take_item takes too much if items arent visible"""
                    self.stash_take_item(item_id, item_amount)
            elif self.STATE == 'TRADE':
                # current_trade_user = ('rompatel_sentinel', ('buy', 'rusted-expedition-scarab', 30, 'chaos-orb', 39), ('2022/06/04', '11:12:18'))
                # inventory_items = [(1290, 613, 10), (1345, 613, 10), (1400, 613, 10)]
                # inventory_items = [(1290, 613, 10)]

                trade_user_name = current_trade_user[0]
                # Decline any invite
                invites = self.check_invite()
                for invite in invites:
                    self.game_invite(invite, accept=False)
                # trade with current_trade_user
                """TODO: bug if no trade_opened will spam trade_invite"""
                self.action_command_chat(self.cmd_tradewith + ' ' + trade_user_name)
                # trade operation
                while self.check_trade_opened():
                    if trade_timer >= 30:
                        print('- Trade attempt limit reached')
                        pyautogui.press('esc')
                        trade_timer = 0
                        self.set_state('HIDEOUT')
                        break
                    self.manage_trade_sell(inventory_items, current_trade_user)
                    trade_opened = True
                    trade_timer += 1
                # trade closed
                if trade_opened:
                    log_result = self.log_manage(time_limit=5)
                    for res in log_result:
                        if 'accepted' in res:
                            print('\n- Trade success')
                            trade_timer = 0
                            trade_opened = False
                            trade_users_done.append(trade_user_name + '%' + str(datetime.now()))
                            trade_users.remove(current_trade_user)
                            self.update_trade_summary(
                                current_trade_user[1][1],  # item_id
                                current_trade_user[1][2],  # item_amount
                                decr=True)
                            if trade_users:
                                current_trade_user = None
                                self.action_command_chat('/kick ' + trade_user_name)
                                self.set_state('START')
                            else:
                                self.action_command_chat('/kick ' + self.char_name)
                                self.set_state(None)
                            break
                        elif 'cancelled' in res:
                            print('- Trade cancelled')
                            trade_opened = False
                            time.sleep(1.5)  # prevent insta trade invite
            time.sleep(self.main_loop_delay)

    def run_buyer(self):
        db_conn = self.db_create_connection()
        current_trade_user = None
        current_currency = None
        in_party = False
        trade_opened = False
        trade_attempt = 0
        trade_started_at = None
        trade_passed = 0
        loading = False
        timer = 0

        while True:
            if not self.STATE:
                current_trade_user = None
                current_currency = None
                trade_opened = False
                trade_attempt = 0
                trade_started_at = None
                trade_passed = 0
                self.set_state(self.STATES['START'])
                continue
            elif self.STATE == 'START':
                """Check if buy msges - switch to run_seller"""
                self.app_window_focus()
                if self.check_hideout() or len(self.check_invite()) > 2:
                    invites = self.check_invite(check_type=True)
                    if invites:
                        if 'party' in invites:  # not sure if this will add preventive trader_switch
                            self.trader_switch = 0
                        in_party = False
                        self.set_state('HIDEOUT')
                        continue
                    if self.check_stash_opened():
                        self.check_remove_alerts()
                        self.check_dump_items()
                        self.fill_from_stash()
                        self.trader_switch = 1
                        if self.check_stash_opened():
                            if self.check_stash_currency():
                                self.trader_switch = 0
                                self.set_state('END')
                                print('- Ended at:', datetime.now().time())
                                self.action_command_chat(self.cmd_logout)
                                break
                            pyautogui.press('esc')
                            self.mouse_move(1000, 300)
                        self.set_state('HIDEOUT')
                        continue
                    else:
                        self.check_open_stash()
                else:
                    if loading:
                        if not self.check_loading():
                            loading = False
                        continue
                    self.action_hideout_tp()
                    log_result = self.log_manage(time_limit=5)
                    for line in log_result:
                        if 'area_error' in line:
                            self.action_command_chat(self.cmd_logout)
                            time.sleep(5)
                            break
                    if self.check_chat_opened():
                        print('- Chat closed')
                        pyautogui.press('enter')
                    loading = True
                    time.sleep(0.5)
            elif self.STATE == 'HIDEOUT':
                if self.check_chat_opened():
                    print('- Chat closed')
                    pyautogui.press('enter')
                if in_party:
                    if self.check_in_party():
                        self.set_state('PRETRADE')
                        continue
                    else:
                        time.sleep(1)  # not sure if this works to fix 1 bug
                        in_party = False
                        self.set_state('START')
                        continue
                else:
                    current_trade_user = None
                    invites = self.check_invite(check_type=True)
                    if not invites and not self.trader_switch:
                        self.trader_switch = 1
                    if 'party' in invites:
                        self.trader_switch = 0
                        invites = self.check_invite(check_type=True)
                    for invite in invites:
                        if 'party' in invite:
                            ocr_text = self.check_invite_account_name(invite)
                            current_trade_user = self.ocr_user_deduct(db_conn, ocr_text)
                            self.game_invite(invite, accept=True)
                            time.sleep(0.3)  # fix double click on different invite
                            if self.check_stash_opened():
                                pyautogui.press('esc')
                            for i in range(3):
                                if self.check_in_party():
                                    in_party = True
                                    self.trader_switch = 0
                                    break
                                time.sleep(0.3)
                            if current_trade_user:
                                break
                        elif 'challenge' in invite:
                            self.game_invite(invite, accept=False)
            elif self.STATE == 'PRETRADE':
                if not self.check_in_party():
                    self.set_state('START')
                    continue
                self.trader_switch = 0
                current_trade_user = self.trade_user_deduct(db_conn, ocr_user=current_trade_user)
                print('- Current user:', current_trade_user)
                if current_trade_user:
                    loading = True
                    while True:
                        print('- Loading...')
                        loading = self.check_loading()
                        if not loading:
                            self.set_state('TRADE')
                            break
                        time.sleep(0.3)
                    continue
                else:
                    print('- Deduct user limit reached!')
                    self.action_command_chat(self.cmd_kick)
                    self.action_command_chat(self.cmd_kick)
                    self.set_state('START')
            elif self.STATE == 'TRADE':
                if not trade_started_at:
                    trade_started_at = datetime.now()
                if not timer % 10:
                    print('- Timer:', timer)
                    if current_currency:
                        self.random_right_click()
                if timer >= self.trade_timer_limit:
                    print('- Trade limit reached: ', timer)
                    trade_opened = False
                    in_party = False
                    timer = 0
                    self.action_command_chat(self.cmd_kick)
                    self.set_state(None)
                    continue
                elif not timer % 5 and timer >= 40:
                    if not self.check_in_party() and not self.check_loading():
                        timer = self.trade_timer_limit

                if not current_currency:
                    current_currency = self.prepare_currency(
                        current_trade_user)
                    continue

                log_result = self.log_manage(time_limit=5)
                for res in log_result:
                    if 'error' in res:
                        self.set_state(None)
                        self.action_command_chat(self.cmd_kick)

                # manage invite
                self.manage_invites(trade=True)
                # manage trade
                while self.check_trade_opened():
                    if trade_attempt >= 40:
                        print('- Trade attempt limit reached')
                        timer = self.trade_timer_limit
                        break
                    trade_opened = True
                    self.manage_trade(current_currency, current_trade_user)
                    trade_attempt += 1

                # manage trade success
                if trade_opened:
                    log_result = self.log_manage(time_limit=5)
                    for res in log_result:
                        if 'accepted' in res:
                            print('- Trade success')
                            trade_opened = False
                            in_party = False
                            timer = 0
                            if current_trade_user[-2] < 15:
                                print('- User priority updated')
                                user_data = (
                                    current_trade_user[-3],
                                    current_trade_user[-2] + 2,
                                    current_trade_user[-1] + 1,
                                    current_trade_user[1]
                                )
                                self.db_update_object(
                                    db_conn,
                                    self.sql_update_trade_user_priority,
                                    user_data
                                )
                            self.update_trade_summary(current_trade_user[4], current_trade_user[7])
                            # self.action_send_ty(char_name=current_trade_user[2])
                            time.sleep(0.3)
                            self.action_command_chat(self.cmd_kick)
                            self.set_state(None)
                            for i in range(6):
                                time.sleep(0.35)
                                self.action_hideout_tp()
                                if self.check_loading():
                                    break
                            break
                        elif 'cancelled' in res:
                            trade_opened = False
                            trade_passed = self.get_datetime_passed_seconds(trade_started_at)
                            print(trade_started_at, ' - ', trade_passed)
                            log_cancelled = [
                                x for x in self.log_manage(time_limit=trade_passed) if x[0] == 'cancelled']
                            if len(log_cancelled) >= 2:
                                timer = self.trade_timer_limit
                                break
                        else:
                            trade_opened = False
                timer += 1
            elif self.STATE == 'END':
                time.sleep(5)
            time.sleep(self.main_loop_delay)
