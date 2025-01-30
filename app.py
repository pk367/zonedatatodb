# @title
import math
import traceback
from tvDatafeed import TvDatafeed, Interval
import pandas as pd
import logging
import pytz
import json
import time
from IPython.display import display, HTML
import mysql.connector

# Initialize logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
def create_db_config(suffix):
    try:
        return {
            'user': f'u417995338_stocksZoneData',
            'password': 'Host@0904',
            'host': 'srv1668.hstgr.io',
            'database': f'u417995338_stocksZoneData',
            'connection_timeout': 100000  # Set connection timeout
        }
    except Exception as e:
        logger.error(f"Error creating DB config: {e}")
        return None
def calculate_atr(stock_data, length=14):
    stock_data['previous_close'] = stock_data['close'].shift(1)
    stock_data['tr1'] = abs(stock_data['high'] - stock_data['low'])
    stock_data['tr2'] = abs(stock_data['high'] - stock_data['previous_close'])
    stock_data['tr3'] = abs(stock_data['low'] - stock_data['previous_close'])
    stock_data['TR'] = stock_data[['tr1', 'tr2', 'tr3']].max(axis=1)

    def rma(series, length):
        alpha = 1 / length
        return series.ewm(alpha=alpha, adjust=False).mean()

    stock_data['ATR'] = rma(stock_data['TR'], length)
    stock_data['Candle_Range'] = stock_data['high'] - stock_data['low']
    stock_data['Candle_Body'] = abs(stock_data['close'] - stock_data['open'])
    return stock_data.round(2)

def capture_ohlc_data(stock_data, exit_index, i):
    #print("Columns in stock_data:", stock_data.columns)
    start_index = max(0, i - 12)
    end_index = min(len(stock_data), exit_index + 12 if exit_index is not None else (i + 12))
    ohlc_data = stock_data.iloc[start_index:end_index]
    ohlc_data = ohlc_data.reset_index().to_dict(orient='records')
    # Convert datetime to string format
    for record in ohlc_data:
        if 'datetime' in record:
            record['datetime'] = record['datetime'].strftime('%Y-%m-%d %H:%M:%S')  # Convert to string

    return ohlc_data

def fetch_data(tv_datafeed, symbol, exchange, interval, n_bars, fut_contract=None):
    """Fetches historical data for the given symbol and interval."""
    try:
        if fut_contract:
            data = tv_datafeed.get_hist(symbol=symbol, exchange=exchange, interval=interval, n_bars=n_bars, fut_contract=fut_contract)
        else:
            data = tv_datafeed.get_hist(symbol=symbol, exchange=exchange, interval=interval, n_bars=n_bars)

        if data is not None and not data.empty:
            data.index = data.index.tz_localize('UTC').tz_convert('Asia/Kolkata')
            data = data.round(2)
        else:
            print(f"No data found for {symbol} on {exchange} with interval {interval}")

        return data
    except Exception as e:
        print(f"Error fetching data for symbol {symbol}: {e}")
        logger.error(f"Error fetching data for symbol {symbol}: {e}")
        return None

def fetch_stock_data_and_resample(symbol, exchange, interval_str, interval,  n_bars, fut_contract):
    """
    Fetches and resamples stock data for a given symbol and interval.
    """
    tv_datafeed = TvDatafeed()

    # Mapping resampling rules
    RULE_MAP = {
            'in_10_minute': '10min',
            'in_75_minute': '75min',
            'in_125_minute': '125min',
            'in_5_hour': '5h',
            'in_6_hour': '6h',
            'in_8_hour': '8h',
            'in_10_hour': '10h',
            'in_12_hour': '12h',
    }

    rule = RULE_MAP.get(interval_str)
    if not rule:
        print(f"Invalid interval_str: {interval_str}. No resampling rule found.")
        logger.error(f"Invalid interval_str: {interval_str}. No resampling rule found.")
        return None, None

    # Fetch initial data
    symbol_data = fetch_data(tv_datafeed, symbol, exchange, interval, n_bars, fut_contract)
    if symbol_data is None or symbol_data.empty:
        print(f"No data found for symbol {symbol} on exchange {exchange} with interval {interval}")
        return None, None

    # Resample the data
    #print(f"Resampling data for {symbol} with rule {rule}")
    symbol_data_resampled = symbol_data.resample(rule=rule, closed='left', label='left').agg({
        'open': 'first',
        'high': 'max',
        'low': 'min',
        'close': 'last',
        'volume': 'sum'
    }).dropna()



    return symbol_data_resampled


def is_overlap_less_than_50(stock_data, legin_candle_index):

    legin_candle_body = stock_data['Candle_Body'].iloc[legin_candle_index]
    previous_candle_body = stock_data['Candle_Body'].iloc[legin_candle_index-1]
    return (previous_candle_body < legin_candle_body * 0.50)

def check_legout_covered(it_is_demand_zone, stock_data, i, entry_index, total_risk, reward_value, first_legout_candle_range, entry_price):
    first_legout_half = first_legout_candle_range * 0.50
    legout_covered_limit = (total_risk * reward_value if reward_value == 3 else 5) + entry_price if it_is_demand_zone else (total_risk * reward_value if reward_value == 3 else 5) - entry_price

    if entry_index is not None:
        if it_is_demand_zone:
            highest_high = stock_data['high'].iloc[i:entry_index + 1].max()
            return highest_high > legout_covered_limit
        else:
            lowest_low = stock_data['low'].iloc[i:entry_index + 1].min()
            return lowest_low < legout_covered_limit
    else:
        crossed = False
        for n in range(i, len(stock_data)):
            if not crossed:
                if (it_is_demand_zone and stock_data['low'].iloc[n] <= first_legout_half) or \
                   (not it_is_demand_zone and stock_data['high'].iloc[n] >= first_legout_half):
                    crossed = True
            else:
                if (it_is_demand_zone and stock_data['high'].iloc[n] > legout_covered_limit) or \
                   (not it_is_demand_zone and stock_data['low'].iloc[n] < legout_covered_limit):
                    return True
        return False


def find_patterns(ticker,exchange, stock_data,  interval_key, max_base_candles,reward_value, scan_demand_zone_allowed, scan_supply_zone_allowed, fresh_zone_allowed, target_zone_allowed, stoploss_zone_allowed):
    try:
        patterns = []
        last_legout_high = []  # Initialize here to avoid error
        last_legout_low = [ ] # Intiialize here to avoid error

        if len(stock_data) < 3:
            print(f"Not enough stock_data for {ticker}")
            return []
        for i in range(len(stock_data) - 1, 2, -1):

            if scan_demand_zone_allowed and (stock_data['close'].iloc[i] > stock_data['open'].iloc[i] and
                stock_data['TR'].iloc[i] > stock_data['ATR'].iloc[i] and
                stock_data['open'].iloc[i] >= stock_data['close'].iloc[i - 1]): # opening of first legout should greater than 0.15% of boring closing

                first_legout_open = stock_data['open'].iloc[i]

                first_legout_candle_body = abs(stock_data['close'].iloc[i] - stock_data['open'].iloc[i])
                first_legout_candle_range = (stock_data['high'].iloc[i] - stock_data['low'].iloc[i])

                if first_legout_candle_body >= 0.5 * first_legout_candle_range:
                    high_prices = []
                    low_prices = []
                    for base_candles_count in range(1, max_base_candles + 1):
                        base_candles_found = 0

                        legin_candle_index = i - (base_candles_count + 1)
                        legin_candle_body = stock_data['Candle_Body'].iloc[legin_candle_index]
                        legin_candle_range = stock_data['Candle_Range'].iloc[legin_candle_index]

                        for k in range(1, base_candles_count + 1):
                            if (stock_data['ATR'].iloc[i - k] > stock_data['TR'].iloc[i - k] and
                               (legin_candle_body >= 0.50 * legin_candle_range) and (stock_data['TR'].iloc[legin_candle_index] > 0.8 * stock_data['ATR'].iloc[legin_candle_index])  ):

                                base_candles_found += 1
                                high_prices.append(stock_data['high'].iloc[i - k])
                                low_prices.append(stock_data['low'].iloc[i - k])

                            max_high_price = max(high_prices) if high_prices else None
                            min_low_price = min(low_prices) if low_prices else None

                            if  max_high_price is not None and min_low_price is not None:
                                actual_base_candle_range = max_high_price - min_low_price
                            actual_legout_candle_range = None
                            first_legout_candle_range_for_one_two_ka_four = (stock_data['high'].iloc[i] - stock_data['close'].iloc[i-1])
                            condition_met = False  # Flag to check if any condition was met


                            if base_candles_found == base_candles_count:
                                if (
                                    legin_candle_range >= 1.5 * actual_base_candle_range and
                                    first_legout_candle_range_for_one_two_ka_four >= 2 * legin_candle_range and

                                    stock_data['low'].iloc[i] >= stock_data['low'].iloc[legin_candle_index]):
                                    legout_count = '1'
                                    condition_met = True  # Set flag if this condition is met
                                    # Add your logic here if needed

                                else:  # This is the else part for the if statement above
                                    last_legout_high = []
                                    j = i + 1
                                    while j in range(i + 1, min(i + 3, len(stock_data))) and stock_data['close'].iloc[j] > stock_data['open'].iloc[j]:
                                        # Check if j == i + 1
                                        if j == i + 1:
                                            if (stock_data['open'].iloc[j] >= 0.10* stock_data['close'].iloc[i] and
                                                stock_data['low'].iloc[j] >= 0.50 * stock_data['Candle_Range'].iloc[i]):
                                                last_legout_high.append(stock_data['high'].iloc[j])

                                        # Check if j == i + 2
                                        elif j == i + 2:
                                            if stock_data['low'].iloc[j] >= stock_data['low'].iloc[i + 1]:
                                                last_legout_high.append(stock_data['high'].iloc[j])

                                        j += 1

                                    last_legout_high_value = max(last_legout_high) if last_legout_high else None

                                    if last_legout_high_value is not None:
                                        actual_legout_candle_range = last_legout_high_value - stock_data['close'].iloc[i - 1]

                                        if (legin_candle_range >= 1.5 * actual_base_candle_range and
                                            actual_legout_candle_range >= 2 * legin_candle_range and
                                            stock_data['low'].iloc[i] >= stock_data['low'].iloc[legin_candle_index]):
                                            legout_count = (j-i)+1
                                            condition_met = True  # Set flag if this condition is met


                            # Code block to execute if any condition was met
                            if condition_met:

                                if interval_key in ('1D','1Wk','1Mo') :
                                    legin_date = stock_data.index[legin_candle_index].strftime('%Y-%m-%d')
                                    legout_date = stock_data.index[i].strftime('%Y-%m-%d')
                                else:
                                    legin_date = stock_data.index[legin_candle_index].strftime('%Y-%m-%d %H:%M:%S')
                                    legout_date = stock_data.index[i].strftime('%Y-%m-%d %H:%M:%S')


                                if actual_legout_candle_range is not None:
                                    legout_candle_range = actual_legout_candle_range
                                else:
                                    legout_candle_range = first_legout_candle_range_for_one_two_ka_four


                                entry_occurred = False
                                target_hit = False
                                stop_loss_hit = False
                                entry_date = None
                                entry_index = None
                                exit_date = None
                                exit_index = None
                                Zone_status = None
                                total_risk = max_high_price - min_low_price
                                minimum_target = (total_risk * reward_value) + max_high_price
                                start_index = j+1 if last_legout_high else i + 1

                                for m in range(start_index, len(stock_data)):
                                    if not entry_occurred:
                                        # Check if the entry condition is met
                                        if stock_data['low'].iloc[m] <= max_high_price:
                                            entry_occurred = True
                                            entry_index = m
                                            entry_date = stock_data.index[m].strftime('%Y-%m-%d %H:%M:%S')
                                            Zone_status = 'Active'  # Set to Active when entry occurs

                                            # Check if the low and high of the current candle exceed the limits
                                            if stock_data['low'].iloc[m] < min_low_price:
                                                stop_loss_hit = True
                                                exit_index = m
                                                exit_date = stock_data.index[m].strftime('%Y-%m-%d %H:%M:%S')
                                                Zone_status = 'Failed'  # Changed from 'Stop loss' to 'Failed'
                                                break
                                            elif stock_data['high'].iloc[m] >= minimum_target:
                                                target_hit = True
                                                exit_index = m
                                                exit_date = stock_data.index[m].strftime('%Y-%m-%d %H:%M:%S')
                                                Zone_status = 'Tested'  # Always set to Tested when target is hit first
                                                # Continue monitoring for potential stop loss after target
                                                for n in range(m + 1, len(stock_data)):
                                                    if stock_data['low'].iloc[n] < min_low_price:
                                                        Zone_status = 'Successful'  # Update to Successful if SL hit after target
                                                        break
                                                break
                                        elif min(stock_data['low'].iloc[start_index:]) > max_high_price:
                                            Zone_status = 'Fresh'
                                    else:
                                        # After entry, check if price hits stop-loss or minimum target
                                        if stock_data['low'].iloc[m] < min_low_price:
                                            stop_loss_hit = True
                                            exit_index = m
                                            exit_date = stock_data.index[m].strftime('%Y-%m-%d %H:%M:%S')
                                            Zone_status = 'Failed'  # Changed from 'Stop loss' to 'Failed'
                                            break
                                        elif stock_data['high'].iloc[m] >= minimum_target:
                                            target_hit = True
                                            exit_index = m
                                            exit_date = stock_data.index[m].strftime('%Y-%m-%d %H:%M:%S')
                                            Zone_status = 'Tested'  # Set to Tested when target is hit
                                            # Continue monitoring for potential stop loss after target
                                            for n in range(m + 1, len(stock_data)):
                                                if stock_data['low'].iloc[n] < min_low_price:
                                                    Zone_status = 'Successful'  # Update to Successful if SL hit after target
                                                    break
                                            break

                                # After the loop, if entry occurred but no other status was set, keep it as Active
                                if entry_occurred and Zone_status == 'Active':
                                    Zone_status = 'Active'

                                # time_in_exit = exit_index - entry_index
                                Pattern_name_is = 'DZ(DBR)' if stock_data['open'].iloc[legin_candle_index] > stock_data['close'].iloc[legin_candle_index] else 'DZ(RBR)'
                                legin_base_legout_ranges = f"{round(legin_candle_range)}:{round(actual_base_candle_range)}:{round(legout_candle_range)}"

                                #ohlc_data = capture_ohlc_data(stock_data, exit_index, i)
                                #print(f"entry date is :{stock_data.index[m]}")
                                pulse_check_start_date = pd.to_datetime(entry_date).strftime('%Y-%m-%d %H:%M:%S') if entry_date is not None else pd.Timestamp.now().strftime('%Y-%m-%d %H:%M:%S')
                                #print(f"Pulse Check Start Date: {(pulse_check_start_date)}")

                                latest_closing_price = round(stock_data['close'].iloc[-1], 2)
                                zone_distance = (math.floor(latest_closing_price) - max(high_prices)) / max(high_prices) * 100

                                if ((fresh_zone_allowed and Zone_status == 'Fresh') or \
                                   (target_zone_allowed and (Zone_status == 'Tested' or Zone_status == 'Successful')) or \
                                   (stoploss_zone_allowed and Zone_status == 'Failed') or \
                                   Zone_status == 'Active'):

                                      white_area_condition = (stock_data['open'].iloc[i] >= stock_data['close'].iloc[i - 1]
                                            if stock_data['close'].iloc[i - 1] > stock_data['open'].iloc[i - 1]
                                            else stock_data['open'].iloc[i] >= stock_data['open'].iloc[i - 1])
                                      opposite_color_exist = ((stock_data['close'].iloc[legin_candle_index] > stock_data['open'].iloc[legin_candle_index] and
                                                stock_data['close'].iloc[legin_candle_index - 1] < stock_data['open'].iloc[legin_candle_index - 1]) or
                                                (stock_data['close'].iloc[legin_candle_index] < stock_data['open'].iloc[legin_candle_index] and
                                                stock_data['close'].iloc[legin_candle_index - 1] > stock_data['open'].iloc[legin_candle_index - 1]))


                                      if white_area_condition:
                                          white_area = 'True'
                                      else:
                                          white_area = 'False'

                                      if opposite_color_exist :
                                          if is_overlap_less_than_50(stock_data, legin_candle_index):
                                             legin_not_covered = 'True'
                                          else:
                                              legin_not_covered = 'False'
                                      else:
                                          legin_not_covered = 'True'

                                      if (first_legout_open <= stock_data['close'].iloc[legin_candle_index] + legin_candle_body):
                                          legout_formation = 'True'
                                      else:
                                          legout_formation = 'False'
                                      if ((stock_data['close'].iloc[legin_candle_index] > stock_data['open'].iloc[legin_candle_index]) and (stock_data['high'].iloc[legin_candle_index] > stock_data['close'].iloc[legin_candle_index])) or ((stock_data['open'].iloc[legin_candle_index] > stock_data['close'].iloc[legin_candle_index]) and (stock_data['low'].iloc[legin_candle_index] < stock_data['close'].iloc[legin_candle_index])):
                                          wick_in_legin = 'True'
                                      else:
                                          wick_in_legin = 'False'

                                      if (stock_data['TR'].iloc[legin_candle_index] > stock_data['ATR'].iloc[legin_candle_index]):
                                         legin_tr_check = 'True'
                                      else:
                                          legin_tr_check = 'False'

                                      if check_legout_covered(True, stock_data, i, entry_index, total_risk, reward_value, first_legout_candle_range, max_high_price):
                                         legout_covered = 'True'
                                      else:
                                          legout_covered = 'False'
                                      if (legin_candle_range >= 2 * actual_base_candle_range) :
                                          one_two_ka_four = 'True'
                                      else:
                                          one_two_ka_four = 'False'
                                      patterns.append({
                                            'symbol': ticker,
                                            'exchange':exchange,
                                            'zone_distance':zone_distance,
                                            'timeframe': interval_key,
                                            'zone_status': Zone_status,
                                            'zone_type': Pattern_name_is,  # corrected
                                            'entry_price': max_high_price,  # corrected
                                            'stop_loss': min_low_price,
                                            'target': minimum_target,
                                            'legin_date': legin_date,
                                            'base_count': base_candles_found,
                                            'legout_count': legout_count,
                                            'legout_date': legout_date,
                                            'entry_date': entry_date,
                                            'exit_date': exit_date,


                                            'is_white_area': white_area,
                                            'legin_not_covered': legin_not_covered,
                                            'is_legout_formation': legout_formation,
                                            'is_wick_in_legin': wick_in_legin,
                                            'is_legin_tr_pass': legin_tr_check,
                                            'is_legout_covered': legout_covered,
                                            'is_one_two_ka_four': one_two_ka_four,

                                            #'ohlc_data': ohlc_data,
                                       })

            if scan_supply_zone_allowed and (stock_data['open'].iloc[i] > stock_data['close'].iloc[i] and
                stock_data['TR'].iloc[i] > stock_data['ATR'].iloc[i] and
                stock_data['open'].iloc[i] <=  stock_data['close'].iloc[i - 1]):

                first_legout_open = stock_data['open'].iloc[i]
                first_legout_candle_body = abs(stock_data['close'].iloc[i] - stock_data['open'].iloc[i])
                first_legout_candle_range = (stock_data['high'].iloc[i] - stock_data['low'].iloc[i])

                if first_legout_candle_body >= 0.5 * first_legout_candle_range:
                    high_prices = []
                    low_prices = []
                    for base_candles_count in range(1, max_base_candles + 1):
                        base_candles_found = 0

                        legin_candle_index = i - (base_candles_count + 1)
                        legin_candle_body = stock_data['Candle_Body'].iloc[legin_candle_index]
                        legin_candle_range = stock_data['Candle_Range'].iloc[legin_candle_index]

                        for k in range(1, base_candles_count + 1):
                            if (stock_data['ATR'].iloc[i - k] > stock_data['TR'].iloc[i - k] and
                                (legin_candle_body >= 0.50 * legin_candle_range)  and (stock_data['TR'].iloc[legin_candle_index] > 0.8 * stock_data['ATR'].iloc[legin_candle_index])):

                                base_candles_found += 1
                                high_prices.append(stock_data['high'].iloc[i - k])
                                low_prices.append(stock_data['low'].iloc[i - k])

                            max_high_price = max(high_prices) if high_prices else None
                            min_low_price = min(low_prices) if low_prices else None

                            if max_high_price is not None and min_low_price is not None:
                                actual_base_candle_range = max_high_price - min_low_price
                            actual_legout_candle_range = None
                            first_legout_candle_range_for_one_two_ka_four = (stock_data['close'].iloc[i-1] - stock_data['low'].iloc[i])
                            condition_met = False  # Flag to check if any condition was met



                            if base_candles_found == base_candles_count:
                                if (legin_candle_range >= 1.5 * actual_base_candle_range and
                                    first_legout_candle_range_for_one_two_ka_four >= 2 * legin_candle_range and

                                    stock_data['high'].iloc[i] <= stock_data['high'].iloc[legin_candle_index]):
                                    condition_met = True  # Set flag if this condition is met
                                    legout_count = 1
                                    # Add your logic here if needed

                                else:  # This is the else part for the if statement above
                                    last_legout_low = []
                                    j = i + 1
                                    while j in range(i + 1, min(i + 3, len(stock_data))) and stock_data['open'].iloc[j] > stock_data['close'].iloc[j]:
                                        # Check if j == i + 1
                                        if j == i + 1:
                                            if (stock_data['open'].iloc[j] <= 0.10* stock_data['close'].iloc[i] and
                                                stock_data['high'].iloc[j] <= 0.50 * stock_data['Candle_Range'].iloc[i]):
                                                last_legout_low.append(stock_data['low'].iloc[j])

                                        # Check if j == i + 2
                                        elif j == i + 2:
                                            if stock_data['high'].iloc[j] <= stock_data['high'].iloc[i + 1]:
                                                last_legout_low.append(stock_data['low'].iloc[j])

                                        j += 1

                                    last_legout_low_value = min(last_legout_low) if last_legout_low else None

                                    if last_legout_low_value is not None:
                                        actual_legout_candle_range = abs(last_legout_low_value - stock_data['close'].iloc[i - 1])

                                        if (legin_candle_range >= 1.5 * actual_base_candle_range and
                                            actual_legout_candle_range >= 2 * legin_candle_range and
                                            stock_data['high'].iloc[i] <= stock_data['high'].iloc[legin_candle_index]):

                                            condition_met = True  # Set flag if this condition is met
                                            legout_count = (j-i)+1

                            # Code block to execute if any condition was met
                            if condition_met:
                                if interval_key in ('1d','1wk','1mo') :
                                    legin_date = stock_data.index[legin_candle_index].strftime('%Y-%m-%d')
                                    legout_date = stock_data.index[i].strftime('%Y-%m-%d')
                                else:
                                    legin_date = stock_data.index[legin_candle_index].strftime('%Y-%m-%d %H:%M:%S')
                                    legout_date = stock_data.index[i].strftime('%Y-%m-%d %H:%M:%S')

                                if actual_legout_candle_range is not None:
                                    legout_candle_range = actual_legout_candle_range
                                else:
                                    legout_candle_range = first_legout_candle_range_for_one_two_ka_four


                                entry_occurred = False
                                target_hit = False
                                stop_loss_hit = False
                                entry_date = None
                                entry_index = None
                                exit_date = None
                                exit_index = None
                                Zone_status = None
                                total_risk = max_high_price - min_low_price
                                minimum_target = min_low_price - (total_risk * reward_value)
                                start_index = j+1 if last_legout_low else i + 1

                                for m in range(start_index, len(stock_data)):
                                    if not entry_occurred:
                                        # Check if the entry condition is met
                                        if stock_data['high'].iloc[m] >= min_low_price:
                                            entry_occurred = True
                                            entry_index = m
                                            entry_date = stock_data.index[m].strftime('%Y-%m-%d %H:%M:%S')
                                            Zone_status = 'Active'  # Set to Active when entry occurs

                                            # Check if the low and high of the current candle exceed the limits
                                            if stock_data['high'].iloc[m] > max_high_price:
                                                stop_loss_hit = True
                                                exit_index = m
                                                exit_date = stock_data.index[m].strftime('%Y-%m-%d %H:%M:%S')
                                                Zone_status = 'Failed'  # Changed from 'Stop loss' to 'Failed'
                                                break
                                            elif stock_data['low'].iloc[m] <= minimum_target:
                                                target_hit = True
                                                exit_index = m
                                                exit_date = stock_data.index[m].strftime('%Y-%m-%d %H:%M:%S')
                                                Zone_status = 'Tested'  # Always set to Tested when target is hit first
                                                # Continue monitoring for potential stop loss after target
                                                for n in range(m + 1, len(stock_data)):
                                                    if stock_data['low'].iloc[n] < min_low_price:
                                                        Zone_status = 'Successful'  # Update to Successful if SL hit after target
                                                        break
                                                break
                                        elif max(stock_data['high'].iloc[start_index:]) < max_high_price:
                                             Zone_status = 'Fresh'
                                    else:
                                        # After entry, check if price hits stop-loss or minimum target
                                        if stock_data['high'].iloc[m] > max_high_price:
                                            stop_loss_hit = True
                                            exit_index = m
                                            exit_date = stock_data.index[m].strftime('%Y-%m-%d %H:%M:%S')
                                            Zone_status = 'Failed'  # Changed from 'Stop loss' to 'Failed'
                                            break
                                        elif stock_data['low'].iloc[m] <= minimum_target:
                                            target_hit = True
                                            exit_index = m
                                            exit_date = stock_data.index[m].strftime('%Y-%m-%d %H:%M:%S')
                                            Zone_status = 'Tested'  # Set to Tested when target is hit
                                            # Continue monitoring for potential stop loss after target
                                            for n in range(m + 1, len(stock_data)):
                                                if stock_data['low'].iloc[n] < min_low_price:
                                                    Zone_status = 'Successful'  # Update to Successful if SL hit after target
                                                    break
                                            break

                                # After the loop, if entry occurred but no other status was set, keep it as Active
                                if entry_occurred and Zone_status == 'Active':
                                    Zone_status = 'Active'

                                Pattern_name_is = 'SZ(RBD)' if stock_data['close'].iloc[legin_candle_index] > stock_data['open'].iloc[legin_candle_index] else 'SZ(DBD)'

                                #ohlc_data = capture_ohlc_data(stock_data, exit_index, i)

                                pulse_check_start_date = pd.to_datetime(entry_date).strftime('%Y-%m-%d %H:%M:%S') if entry_date is not None else pd.Timestamp.now().strftime('%Y-%m-%d %H:%M:%S')
                                #print(f"Pulse Check Start Date: {(pulse_check_start_date)}")

                                latest_closing_price = round(stock_data['close'].iloc[-1], 2)
                                zone_distance = (min(low_prices) - math.floor(latest_closing_price)) / min(low_prices) * 100

                                if ((fresh_zone_allowed and Zone_status == 'Fresh') or \
                                   (target_zone_allowed and (Zone_status == 'Tested' or Zone_status == 'Successful')) or \
                                   (stoploss_zone_allowed and Zone_status == 'Failed') or \
                                   Zone_status == 'Active'):

                                       white_area_condition = (stock_data['open'].iloc[i] <= stock_data['close'].iloc[i - 1]
                                                    if stock_data['close'].iloc[i - 1] < stock_data['open'].iloc[i - 1]
                                                    else stock_data['open'].iloc[i] <= stock_data['open'].iloc[i - 1])
                                       opposite_color_exist = ((stock_data['close'].iloc[legin_candle_index] > stock_data['open'].iloc[legin_candle_index] and
                                                        stock_data['close'].iloc[legin_candle_index - 1] < stock_data['open'].iloc[legin_candle_index - 1]) or
                                                        (stock_data['close'].iloc[legin_candle_index] < stock_data['open'].iloc[legin_candle_index] and
                                                        stock_data['close'].iloc[legin_candle_index - 1] > stock_data['open'].iloc[legin_candle_index - 1]))



                                       if white_area_condition:
                                           white_area = 'True'
                                       else:
                                           white_area = 'False'
                                       if opposite_color_exist :
                                           if is_overlap_less_than_50(stock_data, legin_candle_index):
                                              legin_not_covered = 'True'
                                           else:
                                               legin_not_covered = 'False'
                                       else:
                                          legin_not_covered = 'True'


                                       if (first_legout_open >= stock_data['close'].iloc[legin_candle_index] - legin_candle_body):
                                           legout_formation = 'True'
                                       else:
                                           legout_formation = 'False'
                                       if ((stock_data['close'].iloc[legin_candle_index] > stock_data['open'].iloc[legin_candle_index]) and (stock_data['high'].iloc[legin_candle_index] > stock_data['close'].iloc[legin_candle_index])) or ((stock_data['open'].iloc[legin_candle_index] > stock_data['close'].iloc[legin_candle_index]) and (stock_data['low'].iloc[legin_candle_index] < stock_data['close'].iloc[legin_candle_index])):
                                           wick_in_legin = 'True'
                                       else:
                                           wick_in_legin = 'False'

                                       if (stock_data['TR'].iloc[legin_candle_index] > stock_data['ATR'].iloc[legin_candle_index]):
                                          legin_tr_check = 'True'
                                       else:
                                           legin_tr_check = 'False'

                                       if check_legout_covered(False, stock_data, i, entry_index, total_risk, reward_value, first_legout_candle_range, min_low_price):
                                           legout_covered = 'True'
                                       else:
                                           legout_covered = 'False'
                                       if (legin_candle_range >= 2 * actual_base_candle_range) :
                                          one_two_ka_four = 'True'
                                       else:
                                           one_two_ka_four = 'False'

                                       patterns.append({
                                            'symbol': ticker,
                                            'exchange': exchange,
                                            'zone_distance':zone_distance,
                                            'timeframe': interval_key,
                                            'zone_status': Zone_status,
                                            'zone_type': Pattern_name_is,  # corrected
                                            'entry_price': min_low_price,  # corrected
                                            'stop_loss': max_high_price,
                                            'target': minimum_target,
                                            'legin_date': legin_date,
                                            'base_count': base_candles_found,
                                            'legout_count': legout_count,
                                            'legout_date': legout_date,
                                            'entry_date': entry_date,
                                            'exit_date': exit_date,

                                            'is_white_area': white_area,
                                            'legin_not_covered': legin_not_covered,
                                            'is_legout_formation': legout_formation,
                                            'is_wick_in_legin': wick_in_legin,
                                            'is_legin_tr_pass': legin_tr_check,
                                            'is_legout_covered': legout_covered,
                                            'is_one_two_ka_four': one_two_ka_four,

                                            #'ohlc_data': ohlc_data,
                                        })

        return patterns
    except Exception as e:
        print(f"Error processing {ticker}: {e}")
        logger.error(f"Error processing {ticker}: {e}")
        print(traceback.format_exc())  # Correct usage
        return []


def batch_insert_candles(cursor, data_to_insert):
    batch_size = 3000  # Adjust this size as necessary
    for i in range(0, len(data_to_insert), batch_size):
        batch = data_to_insert[i:i + batch_size]
        insert_query = """
            INSERT INTO zone_data (
                symbol, exchange, zone_distance,timeframe,
                zone_status, zone_type, entry_price, stop_loss, target,
                legin_date, base_count, legout_count, legout_date,
                entry_date, exit_date,
                is_white_area, legin_not_covered, is_legout_formation,
                is_wick_in_legin, is_legin_tr_pass, is_legout_covered,
                is_one_two_ka_four
            )
            VALUES ( %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                zone_distance = VALUES(zone_distance),
                entry_price = VALUES(entry_price),
                stop_loss = VALUES(stop_loss),
                target = VALUES(target),
                legin_date = VALUES(legin_date),
                base_count = VALUES(base_count),
                legout_date = VALUES(legout_date),
                exit_date = VALUES(exit_date),
                zone_status = VALUES(zone_status),
                zone_type = VALUES(zone_type),
                is_white_area = VALUES(is_white_area),
                legin_not_covered = VALUES(legin_not_covered),
                is_legout_formation = VALUES(is_legout_formation),
                is_wick_in_legin = VALUES(is_wick_in_legin),
                is_legin_tr_pass = VALUES(is_legin_tr_pass),
                is_legout_covered = VALUES(is_legout_covered),
                legout_count = VALUES(legout_count),
                is_one_two_ka_four = VALUES(is_one_two_ka_four)
        """
        try:
            cursor.executemany(insert_query, batch)
            #logger.info(f"Inserted {len(batch)} rows of pattern data.")
        except mysql.connector.Error as err:
            logger.error(f"Database error during batch insert: {err}")
            print(f"Database error during batch insert: {err}")
            raise
        except Exception as e:
            logger.error(f"Error during batch insert: {e}")
            print(f"Error during batch insert: {e}")
            raise

def fetch_data_endpoint():
    exchange = "NSE"
    # List of symbols
    symbols = [ 'ADANIENSOL',
    'AARTIIND', 'ABB', 'ADANIGREEN', 'ABBOTINDIA', 'ABCAPITAL', 'ANGELONE',
    'ABFRL', 'APLAPOLLO', 'ACC', 'ADANIENT', 'ATGL', 'ADANIPORTS', 'ALKEM',
    'AMBUJACEM', 'APOLLOHOSP', 'APOLLOTYRE', 'ASHOKLEY', 'ASIANPAINT', 'BANKINDIA',
    'BEL', 'BSE', 'ASTRAL', 'ATUL', 'AUBANK', 'AUROPHARMA', 'AXISBANK', 'BERGEPAINT',
    'CAMS', 'CDSL', 'BAJAJ_AUTO', 'BAJAJFINSV', 'COFORGE', 'CESC', 'CGPOWER', 'CUB',
    'BAJFINANCE', 'DIVISLAB', 'DRREDDY', 'COROMANDEL', 'HCLTECH', 'BALKRISIND',
    'BANDHANBNK', 'BANKBARODA', 'BATAINDIA', 'CUMMINSIND', 'CYIENT', 'DIXON',
    'DELHIVERY', 'BHARATFORG', 'BHARTIARTL', 'DLF', 'GODREJCP', 'GODREJPROP',
    'GRANULES', 'HDFCAMC', 'HDFCLIFE', 'HEROMOTOCO', 'ICICIPRULI', 'IGL', 'INDHOTEL',
    'INFY', 'IRB', 'IRFC', 'JINDALSTEL', 'JKCEMENT', 'JSL', 'JSWENERGY', 'JSWSTEEL',
    'JUBLFOOD', 'M_M', 'LAURUSLABS', 'MCX', 'LICHSGFIN', 'LICI', 'LODHA', 'LUPIN',
    'DMART', 'PNB', 'BHEL', 'BOSCHLTD', 'BSOFT', 'HFCL', 'MAXHEALTH', 'CANBK',
    'HUDCO', 'METROPOLIS', 'INDIANB', 'MUTHOOTFIN', 'JIOFIN', 'KALYANKJIL', 'NAUKRI',
    'KEI', 'KPITTECH', 'BIOCON', 'EICHERMOT', 'NCC', 'NHPC', 'NYKAA', 'OBEROIRLTY',
    'OFSS', 'OIL', 'PAGEIND', 'CIPLA', 'PAYTM', 'PETRONET', 'COLPAL', 'CROMPTON',
    'POLICYBZR', 'POLYCAB', 'POONAWALLA', 'BPCL', 'CANFINHOME', 'DALBHARAT',
    'CHOLAFIN', 'PRESTIGE', 'GUJGASLTD', 'HINDCOPPER', 'DEEPAKNTR', 'BRITANNIA',
    'CHAMBLFERT', 'INDIAMART', 'INDUSINDBK', 'IPCALAB', 'DABUR', 'RAMCOCEM',
    'EXIDEIND', 'GAIL', 'GNFC', 'LTIM', 'FEDERALBNK', 'GRASIM', 'GMRAIRPORT',
    'SHREECEM', 'HINDPETRO', 'NATIONALUM', 'COALINDIA', 'MRF', 'CONCOR', 'NMDC',
    'IEX', 'HAL', 'ESCORTS', 'IOC', 'ITC', 'HDFCBANK', 'GLENMARK', 'SUNPHARMA',
    'HAVELLS', 'HINDALCO', 'HINDUNILVR', 'ICICIBANK', 'ICICIGI', 'SUPREMEIND',
    'IDEA', 'IDFCFIRSTB', 'INDIGO', 'INDUSTOWER', 'IRCTC', 'KOTAKBANK', 'LALPATHLAB',
    'MGL', 'LT', 'LTF', 'TATAELXSI', 'LTTS', 'M_MFIN', 'MANAPPURAM', 'MARICO',
    'MARUTI', 'MFSL', 'POWERGRID', 'MOTHERSON', 'MPHASIS', 'NAVINFLUOR', 'NESTLEIND',
    'SJVN', 'SONACOMS', 'PVRINOX', 'RELIANCE', 'ONGC', 'TECHM', 'SBILIFE', 'TIINDIA',
    'SBICARD', 'SBIN', 'SIEMENS', 'UNIONBANK', 'VBL', 'TATASTEEL', 'YESBANK',
    'ZOMATO', 'SUNTV', 'ZYDUSLIFE', 'TATACHEM', 'TATAPOWER', 'TRENT', 'ULTRACEMCO',
    'VOLTAS', 'UNITDSPR', 'NTPC', 'TITAN', 'PERSISTENT', 'PFC', 'RBLBANK', 'SAIL',
    'SRF', 'PEL', 'PIDILITIND', 'PIIND', 'RECLTD', 'SHRIRAMFIN', 'SYNGENE', 'TATACOMM',
    'TATACONSUM', 'TATAMOTORS', 'TCS', 'TORNTPHARM', 'TVSMOTOR', 'UBL', 'UPL', 'VEDL',
    'WIPRO'
    ]
    #symbols = symbolslist[50:]
    # List of time frames
    intervals = [ "in_1_minute", "in_3_minute" , "in_5_minute", "in_10_minute", "in_15_minute", "in_30_minute", "in_75_minute", "in_125_minute", "in_1_hour", "in_2_hour", "in_daily", "in_weekly", "in_monthly" ]

    n_bars = 5000
    fut_contract = None

    INTERVAL_MAP = {
        "in_1_minute": Interval.in_1_minute,
        "in_3_minute": Interval.in_3_minute,
        "in_5_minute": Interval.in_5_minute,
        "in_10_minute": Interval.in_5_minute,
        "in_15_minute": Interval.in_15_minute,
        "in_30_minute": Interval.in_30_minute,
        "in_45_minute": Interval.in_45_minute,
        "in_75_minute": Interval.in_15_minute,
        "in_125_minute": Interval.in_5_minute,
        "in_1_hour": Interval.in_1_hour,
        "in_2_hour": Interval.in_2_hour,
        "in_3_hour": Interval.in_3_hour,
        "in_4_hour": Interval.in_4_hour,
        "in_5_hour": Interval.in_1_hour,
        "in_6_hour": Interval.in_3_hour,
        "in_8_hour": Interval.in_4_hour,
        "in_10_hour": Interval.in_1_hour,
        "in_12_hour": Interval.in_1_hour,
        "in_daily": Interval.in_daily,
        "in_weekly": Interval.in_weekly,
        "in_monthly": Interval.in_monthly,
    }


    reward_mapping = {
        'in_1_minute': 3,
        'in_3_minute': 3,
        'in_5_minute': 3,
        'in_daily': 10,
        'in_weekly': 10,
        'in_monthly': 10
    }

    max_base_candles = 3
    fresh_zone_allowed = True
    target_zone_allowed = True
    stoploss_zone_allowed = True
    scan_demand_zone_allowed = True
    scan_supply_zone_allowed = True

    tv_datafeed = TvDatafeed()  # Initialize TvDatafeed once

    for interval in intervals:
      all_patterns = []
      connections = {}
      cursors = {}

      print(f"processing {interval}")
      logger.info(f"processing {interval}")

      interval_enum = INTERVAL_MAP.get(interval)

      reward_value = reward_mapping.get(interval, 5)

      for sym in symbols:
          print(f"scaning in {sym}")
          try:
              if interval in ['in_10_minute','in_75_minute', 'in_125_minute',"in_5_hour","in_6_hour","in_8_hour","in_10_hour","in_12_hour"]:

                  stock_data = fetch_stock_data_and_resample(sym, exchange, interval, interval_enum, n_bars, fut_contract)
              else:
                  stock_data = fetch_data(tv_datafeed, sym, exchange, interval_enum, n_bars, fut_contract)

              if stock_data is not None and not stock_data.empty:
                  stock_data = calculate_atr(stock_data)
                  stock_data = stock_data.drop(columns=['tr1', 'tr2', 'tr3', 'previous_close'], errors='ignore')

                  patterns = find_patterns(
                      sym,exchange, stock_data, interval,
                      max_base_candles, reward_value,
                      scan_demand_zone_allowed, scan_supply_zone_allowed,
                      fresh_zone_allowed, target_zone_allowed,
                      stoploss_zone_allowed
                  )

                  if patterns:
                      # print(f"{len(patterns)}")
                      all_patterns.extend(patterns)
                      #all_zones.extend(patterns)
                      #print(f"{len(patterns)} zones found in {sym}")

          except Exception as ticker_error:
              print(f"Error processing ticker {sym}: {ticker_error}")
              print(traceback.format_exc())
              result[sym] = {"error": str(ticker_error)}
              logger.error(f"Error processing ticker {sym}: {ticker_error}")

      if all_patterns:
          print(f"{len(all_patterns)} zones found")
          logger.info(f"{len(all_patterns)} zones found")
          # Process collected patterns
          try:
              df = pd.DataFrame(all_patterns)
              df.fillna(0, inplace=True)
              data_to_insert = [tuple(row) for row in df.values]
              #data_to_insert = [ (*row[:-1], json.dumps(row[-1]))  # Convert the last column (ohlc_data) to JSON
              # for row in data_to_insert
              #  ]
              # Create zonedata connection
              config = create_db_config(interval)
              connections['zone'] = mysql.connector.connect(**config)
              cursors['zone'] = connections['zone'].cursor()
              # Create table if not exists
              create_table_query = """
              CREATE TABLE IF NOT EXISTS zone_data (
                  id INT AUTO_INCREMENT PRIMARY KEY,
                  symbol VARCHAR(20),
                  zone_distance DECIMAL(10, 2),
                  exchange VARCHAR(20),
                  timeframe VARCHAR(20),
                  zone_status VARCHAR(10),
                  zone_type VARCHAR(8),
                  entry_price DECIMAL(10, 2),
                  stop_loss DECIMAL(10, 2),
                  target DECIMAL(10, 2),
                  legin_date DATETIME,
                  base_count INT,
                  legout_count INT,
                  legout_date DATETIME,
                  entry_date DATETIME,
                  exit_date DATETIME,
                  is_white_area VARCHAR(6),
                  legin_not_covered VARCHAR(6),
                  is_legout_formation VARCHAR(6),
                  is_wick_in_legin VARCHAR(6),
                  is_legin_tr_pass VARCHAR(6),
                  is_legout_covered VARCHAR(6),
                  is_one_two_ka_four VARCHAR(6),
                  UNIQUE KEY unique_pattern (symbol, timeframe, entry_date, zone_type)
              );
              """
              cursors['zone'].execute(create_table_query)

              # Insert data
              batch_insert_candles(cursors['zone'], data_to_insert)
              connections['zone'].commit()
              print(f"Total {len(df)} zone data successfully uploaded to database.")
              logger.info(f"Total {len(df)} zone data successfully uploaded to database.")

          except Exception as db_error:
              print(f"Database operation error: {db_error}")
              logger.error(f"Database operation error: {db_error}")
              print(traceback.format_exc())
              if 'zone' in connections:
                  connections['zone'].rollback()

          finally:
              if 'zone' in cursors and cursors['zone']:
                  cursors['zone'].close()
              if 'zone' in connections and connections['zone']:
                  connections['zone'].close()

if __name__ == "__main__":
    result = fetch_data_endpoint()
    print(result)
    print("done")
