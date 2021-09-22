import json
import time
import math;

import sys

from flask import request, jsonify

from google.cloud import bigquery
from google.cloud import datastore

from uniswap.utils import calculate_marginal_rate
from uniswap.utils import load_exchange_info
from uniswap.utils import load_exchange_cache

from eth_utils import (
    add_0x_prefix,
    apply_to_return_value,
    from_wei,
    is_address,
    is_checksum_address,
    keccak as eth_utils_keccak,
    remove_0x_prefix,
    to_checksum_address,
    to_wei,
)

# TODO refactor this into a single location
PROJECT_ID = "uniswap-analytics"

EXCHANGES_DATASET_ID = "exchanges_v1"

TICKER_NUM_HOURS = 24

CACHE_DURATION_SECONDS = 60 * 10 # 10 minutes for ticker cache

# return summary data for an exchange for past TICKER_NUM_HOURS hours
def v1_ticker():
	exchange_address = request.args.get("exchangeAddress");
	
	if (exchange_address is None):
		return jsonify(error='missing parameter: exchangeAddress'), 400

	# use current time as end time
	end_time = int(time.time());

	ds_client = datastore.Client();

	# load the datastore cache
	exchange_cache = load_exchange_cache(ds_client, exchange_address);

	use_cache = False;

	# check how old the cache is
	if ((exchange_cache is None) == False):
		elapsed = end_time - exchange_cache["last_updated"];

		# use the cache
		if (elapsed <= CACHE_DURATION_SECONDS):
			print("using cache for " + exchange_address);
			use_cache = True;

	# load the datastore exchange info
	exchange_info = load_exchange_info(ds_client, exchange_address);

	if (exchange_info == None):
		return jsonify(error='no exchange found for this address'), 404

	eth_liquidity = int(exchange_info["cur_eth_total"]);
	erc20_liquidity = int(exchange_info["cur_tokens_total"]);
	
	# grab these values from the cache if we should
	if (use_cache):
		end_time = exchange_cache["end_time"];

		start_time = exchange_cache["start_time"];

		end_exchange_rate = exchange_cache["end_exchange_rate"];

		start_exchange_rate = exchange_cache["start_exchange_rate"];

		eth_trade_volume = exchange_cache["eth_trade_volume"];

		weighted_avg_price_total = exchange_cache["weighted_avg_price_total"];

		highest_price = exchange_cache["highest_price"];
		lowest_price = exchange_cache["lowest_price"];

		last_trade_price = exchange_cache["last_trade_price"];

		last_trade_eth_qty = exchange_cache["last_trade_eth_qty"];
		last_trade_erc20_qty = exchange_cache["last_trade_erc20_qty"];

		num_transactions = exchange_cache["num_transactions"];
	else:
		# floor to last 15 minute mark. this ensures that all SQL queries in 15 minute windows are the same and use cached BigQuery results
		# end_time = math.floor(end_time / 900) * 900;
		
		# pull logs from TICKER_NUM_HOURS hours ago
		start_time = end_time - (60 * 60 * TICKER_NUM_HOURS);

		# pull the transactions from this exchange
		bq_client = bigquery.Client()

		exchange_table_id = "exchange_history_" + to_checksum_address(exchange_address);
	 
		exchange_table_name = "`" + PROJECT_ID + "." + EXCHANGES_DATASET_ID + "." + exchange_table_id + "`"

		bq_query_sql = """
	         SELECT 
	         		tx_hash,
	        		CAST(event as STRING) as event, CAST(timestamp as INT64) as timestamp,
	        		CAST(eth as STRING) as eth, CAST(tokens as STRING) as tokens,
	        		CAST(cur_eth_total as STRING) as eth_liquidity, CAST(cur_tokens_total as STRING) as tokens_liquidity
	         FROM """ + exchange_table_name + """
	          WHERE (timestamp >= """ + str(start_time) + """ and timestamp <= """ + str(end_time) + """)""" + """ group by event, timestamp, eth, tokens, cur_eth_total, cur_tokens_total, tx_hash """ + """ order by timestamp desc, tx_hash asc"""

		print(bq_query_sql)

		# query all the blocks and their associated timestamps
		exchange_query = bq_client.query(bq_query_sql)

		exchange_results = exchange_query.result();

		start_exchange_rate = -1;
		end_exchange_rate = -1;
		
		highest_price = -1;
		lowest_price = sys.maxsize;

		num_transactions = 0;

		eth_trade_volume = 0;
		
		last_trade_price = 0;
		last_trade_eth_qty = 0;
		last_trade_erc20_qty = 0;

		weighted_avg_price_total = 0;

		# iterate through the results from oldest to newest (timestamp asc)
		for row in exchange_results:
			row_event = row.get("event");
			row_eth = int(row.get("eth"));
			row_eth_liquidity = int(row.get("eth_liquidity"));

			row_tokens = int(row.get("tokens"));
			row_tokens_liquidity = int(row.get("tokens_liquidity"));

			# the exchange rate after this transaction was executed
			exchange_rate_after_transaction = calculate_marginal_rate(row_eth_liquidity, row_tokens_liquidity);
			# the exchange rate before this transaction was executed
			exchange_rate_before_transaction = calculate_marginal_rate(row_eth_liquidity - row_eth, row_tokens_liquidity - row_tokens);	

			# track highest price
			if (exchange_rate_after_transaction > highest_price):
				highest_price = exchange_rate_after_transaction;

			# track lowest price
			if (exchange_rate_after_transaction < lowest_price):
				lowest_price = exchange_rate_after_transaction;

			# if we haven't set a start price yet, take the exchange rate before this transaction
			if (start_exchange_rate < 0):
				start_exchange_rate = exchange_rate_before_transaction

			# override the end_price with each transaction to get the latest
			end_exchange_rate = exchange_rate_after_transaction;

			num_transactions += 1;

			if (row_event == "EthPurchase" or row_event == "TokenPurchase"):
				eth_trade_volume += abs(row_eth);

				last_trade_price = exchange_rate_before_transaction;

				last_trade_eth_qty = row_eth;
				last_trade_erc20_qty = row_tokens;

				# for calculating average weighted price, take the amount of eth times the rate that they traded at
				weighted_avg_price_total += (abs(row_eth) * exchange_rate_before_transaction);

		# calculate average weighted price
		if (eth_trade_volume != 0):
			weighted_avg_price_total = weighted_avg_price_total / eth_trade_volume;

		# update the cache
		exchange_cache["end_time"] = end_time;
		exchange_cache["start_time"] = start_time;

		exchange_cache["end_exchange_rate"] = end_exchange_rate;
		exchange_cache["start_exchange_rate"] = start_exchange_rate;

		exchange_cache["eth_trade_volume"] = str(eth_trade_volume);
		exchange_cache["weighted_avg_price_total"] = weighted_avg_price_total;

		exchange_cache["highest_price"] = highest_price;
		exchange_cache["lowest_price"] = lowest_price;

		exchange_cache["last_trade_price"] = last_trade_price;

		exchange_cache["last_updated"] = end_time;

		exchange_cache["last_trade_eth_qty"] = str(last_trade_eth_qty);
		exchange_cache["last_trade_erc20_qty"] = str(last_trade_erc20_qty);

		exchange_cache["num_transactions"] = num_transactions;

		ds_client.put(exchange_cache)

	price_change = end_exchange_rate - start_exchange_rate;
	price_change_percent = price_change / start_exchange_rate;

	marginal_rate = calculate_marginal_rate(eth_liquidity, erc20_liquidity);
	
	inv_marginal_rate = 1 / marginal_rate;

	result = {
		"symbol" : exchange_info["symbol"],

		"startTime" : start_time,
		"endTime" : end_time,
		
		"price" : marginal_rate,
		"invPrice" : inv_marginal_rate,
		
		"highPrice" : highest_price,
		"lowPrice" : lowest_price,
		"weightedAvgPrice" : weighted_avg_price_total,

		"priceChange" : price_change,
		"priceChangePercent" : price_change_percent,		

		"ethLiquidity" : str(eth_liquidity),
		"erc20Liquidity" : str(erc20_liquidity),

		"lastTradePrice" : last_trade_price,
		"lastTradeEthQty" : str(last_trade_eth_qty),
		"lastTradeErc20Qty" : str(last_trade_erc20_qty),

		"tradeVolume" : str(eth_trade_volume),
		"count" : num_transactions
	}

	if ("theme" in exchange_info):
		result["theme"] = exchange_info["theme"];
		
	return jsonify(result)
