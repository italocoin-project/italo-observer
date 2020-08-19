#!/usr/bin/env python3

import flask
from datetime import datetime
import babel.dates
import json
import sys
import statistics

import config
from lmq import FutureJSON, lmq_connection

# Make a dict of config.* to pass to templating
conf = {x: getattr(config, x) for x in dir(config) if not x.startswith('__')}

app = flask.Flask(__name__)
if __name__ == '__main__':
    # DEBUG:
    app.config['TEMPLATES_AUTO_RELOAD'] = True
    app.jinja_env.auto_reload = True

@app.template_filter('format_datetime')
def format_datetime(value, format='long'):
    return babel.dates.format_datetime(value, format, tzinfo=babel.dates.get_timezone('UTC'))

@app.template_filter('from_timestamp')
def from_timestamp(value):
    return datetime.fromtimestamp(value)

@app.template_filter('ago')
def datetime_ago(value):
    delta = datetime.now() - value
    disp=''
    if delta.days < 0:
        delta = -delta
        disp += '-'
    if delta.days > 0:
        disp += '{}d '.format(delta.days)
    disp += '{:2d}:{:02d}:{:02d}'.format(delta.seconds // 3600, delta.seconds // 60 % 60, delta.seconds % 60)
    return disp


@app.template_filter('reltime')
def relative_time(seconds):
    ago = False
    if seconds == 0:
        return 'now'
    elif seconds < 0:
        seconds = -seconds
        ago = True

    if seconds < 90:
        delta = '{:.0f} seconds'.format(seconds)
    elif seconds < 90 * 60:
        delta = '{:.1f} minutes'.format(seconds / 60)
    elif seconds < 36 * 3600:
        delta = '{:.1f} hours'.format(seconds / 3600)
    elif seconds < 99.5 * 86400:
        delta = '{:.1f} days'.format(seconds / 86400)
    else:
        delta = '{:.0f} days'.format(seconds / 86400)

    return delta + ' ago' if ago else 'in ' + delta


@app.template_filter('roundish')
def filter_round(value):
    return ("{:.0f}" if value >= 100 or isinstance(value, int) else "{:.1f}" if value >= 10 else "{:.2f}").format(value)

@app.template_filter('chop0')
def filter_chop0(value):
    value = str(value)
    if '.' in value:
        return value.rstrip('0').rstrip('.')
    return value

si_suffix = ['', 'k', 'M', 'G', 'T', 'P', 'E', 'Z', 'Y']
@app.template_filter('si')
def format_si(value):
    i = 0
    while value >= 1000 and i < len(si_suffix) - 1:
        value /= 1000
        i += 1
    return filter_round(value) + '{}'.format(si_suffix[i])

@app.template_filter('loki')
def format_loki(atomic, tag=True, fixed=False, decimals=9):
    """Formats an atomic current value as a human currency value.
    tag - if False then don't append " LOKI"
    fixed - if True then don't strip insignificant trailing 0's and '.'
    decimals - at how many decimal we should round; the default is full precision
    """
    disp = "{{:.{}f}}".format(decimals).format(atomic * 1e-9)
    if not fixed and decimals > 0:
        disp = disp.rstrip('0').rstrip('.')
    if tag:
        disp += ' LOKI'
    return disp

@app.after_request
def add_global_headers(response):
    if 'Cache-Control' not in response.headers:
        response.headers['Cache-Control'] = 'no-store'
    return response

@app.route('/style.css')
def css():
    return flask.send_from_directory('static', 'style.css')


def get_sns_future(lmq, lokid):
    return FutureJSON(lmq, lokid, 'rpc.get_service_nodes', 5,
            args=[json.dumps({
                'all': False,
                'fields': { x: True for x in ('service_node_pubkey', 'requested_unlock_height', 'last_reward_block_height',
                    'last_reward_transaction_index', 'active', 'funded', 'earned_downtime_blocks',
                    'service_node_version', 'contributors', 'total_contributed', 'total_reserved',
                    'staking_requirement', 'portions_for_operator', 'operator_address', 'pubkey_ed25519',
                    'last_uptime_proof', 'service_node_version') } }).encode()])

def get_sns(sns_future, info_future):
    info = info_future.get()
    awaiting_sns, active_sns, inactive_sns = [], [], []
    sn_states = sns_future.get()['service_node_states']
    for sn in sn_states:
        sn['contribution_open'] = sn['staking_requirement'] - sn['total_reserved']
        sn['contribution_required'] = sn['staking_requirement'] - sn['total_contributed']
        sn['num_contributions'] = sum(len(x['locked_contributions']) for x in sn['contributors'])

        if sn['active']:
            active_sns.append(sn)
        elif sn['funded']:
            sn['decomm_blocks_remaining'] = max(sn['earned_downtime_blocks'], 0)
            sn['decomm_blocks'] = info['height'] - sn['state_height']
            inactive_sns.append(sn)
        else:
            awaiting_sns.append(sn)
    return awaiting_sns, active_sns, inactive_sns

def template_globals():
    return {
        'config': conf,
        'server': { 'timestamp': datetime.utcnow() }
    }


@app.route('/')
@app.route('/page/<int:page>')
@app.route('/page/<int:page>/<int:per_page>')
@app.route('/range/<int:first>/<int:last>')
@app.route('/autorefresh/<int:refresh>')
def main(refresh=None, page=0, per_page=None, first=None, last=None):
    lmq, lokid = lmq_connection()
    inforeq = FutureJSON(lmq, lokid, 'rpc.get_info', 1)
    stake = FutureJSON(lmq, lokid, 'rpc.get_staking_requirement', 10)
    base_fee = FutureJSON(lmq, lokid, 'rpc.get_fee_estimate', 10)
    hfinfo = FutureJSON(lmq, lokid, 'rpc.hard_fork_info', 10)
    mempool = FutureJSON(lmq, lokid, 'rpc.get_transaction_pool', 5)
    sns = get_sns_future(lmq, lokid)

    # This call is slow the first time it gets called in lokid but will be fast after that, so call
    # it with a very short timeout.  It's also an admin-only command, so will always fail if we're
    # using a restricted RPC interface.
    coinbase = FutureJSON(lmq, lokid, 'admin.get_coinbase_tx_sum', 10, timeout=1, fail_okay=True,
            args=[json.dumps({"height":0, "count":2**31-1}).encode()])

    custom_per_page = ''
    if per_page is None or per_page <= 0 or per_page > config.max_blocks_per_page:
        per_page = config.blocks_per_page
    else:
        custom_per_page = '/{}'.format(per_page)

    # We have some chained request dependencies here and below, so get() them as needed; all other
    # non-dependent requests should already have a future initiated above so that they can
    # potentially run in parallel.
    info = inforeq.get()
    height = info['height']

    # Permalinked block range:
    if first is not None and last is not None and 0 <= first <= last and last <= first + 99:
        start_height, end_height = first, last
        if end_height - start_height + 1 != per_page:
            per_page = end_height - start_height + 1;
            custom_per_page = '/{}'.format(per_page)
        # We generally can't get a perfect page number because our range (e.g. 5-14) won't line up
        # with pages (e.g. 10-19, 0-19), so just get as close as we can.  Next/Prev page won't be
        # quite right, but they'll be within half a page.
        page = round((height - 1 - end_height) / per_page)
    else:
        end_height = max(0, height - per_page*page - 1)
        start_height = max(0, end_height - per_page + 1)

    blocks = FutureJSON(lmq, lokid, 'rpc.get_block_headers_range', args=[json.dumps({
        'start_height': start_height,
        'end_height': end_height,
        'get_tx_hashes': True,
        }).encode()]).get()['headers']

    # If 'txs' is already there then it is probably left over from our cached previous call through
    # here.
    if blocks and 'txs' not in blocks[0]:
        txids = []
        for b in blocks:
            b['txs'] = []
            txids.append(b['miner_tx_hash'])
            if 'tx_hashes' in b:
                txids += b['tx_hashes']
        txs = FutureJSON(lmq, lokid, 'rpc.get_transactions', args=[json.dumps({
            "txs_hashes": txids,
            "decode_as_json": True,
            "tx_extra": True,
            "prune": True,
            }).encode()]).get()
        txs = txs['txs']
        i = 0
        for tx in txs:
            # TXs should come back in the same order so we can just skip ahead one when the block
            # height changes rather than needing to search for the block
            if blocks[i]['height'] != tx['block_height']:
                i += 1
                while i < len(blocks) and blocks[i]['height'] != tx['block_height']:
                    print("Something getting wrong: missing txes?", file=sys.stderr)
                    i += 1
                if i >= len(blocks):
                    print("Something getting wrong: have leftover txes")
                    break
            tx['info'] = json.loads(tx['as_json'])
            blocks[i]['txs'].append(tx)


    #txes = FutureJSON(lmq, lokid, 'rpc.get_transactions');


    # mempool RPC return values are about as nasty as can be.  For each mempool tx, we get back
    # *both* binary+hex encoded values and JSON-encoded values slammed into a string, which means we
    # have to invoke an *extra* JSON parser for each tx.  This is terrible.
    mp = mempool.get()
    if 'transactions' in mp:
        for tx in mp['transactions']:
            tx['info'] = json.loads(tx["tx_json"])
    else:
        mp['transactions'] = []

    # Clean up the SN data a bit to make things easier for the templates
    awaiting_sns, active_sns, inactive_sns = get_sns(sns, inforeq)

    return flask.render_template('index.html',
            info=info,
            stake=stake.get(),
            fees=base_fee.get(),
            emission=coinbase.get(),
            hf=hfinfo.get(),
            active_sns=active_sns,
            inactive_sns=inactive_sns,
            awaiting_sns=awaiting_sns,
            blocks=blocks,
            block_size_median=statistics.median(b['block_size'] for b in blocks),
            page=page,
            per_page=per_page,
            custom_per_page=custom_per_page,
            mempool=mp,
            refresh=refresh,
            **template_globals(),
            )

@app.route('/service_nodes')
def sns():
    lmq, lokid = lmq_connection()
    info = FutureJSON(lmq, lokid, 'rpc.get_info', 1)
    awaiting, active, inactive = get_sns(get_sns_future(lmq, lokid), info)

    return flask.render_template('service_nodes.html',
        info=info.get(),
        active_sns=active,
        awaiting_sns=awaiting,
        inactive_sns=inactive,
        **template_globals(),
        )