#!/usr/bin/env python3

# NOTE: Due to a weird bug, numpy is detected as a 3rd party module, while lmdb
#       is not and pylint complains about wrong-import-order.
#       Since these checks have to be disabled for matplotlib imports anyway, they
#       were moved a bit higher up to avoid the issue.
# pylint: disable=wrong-import-order,wrong-import-position
import argparse
import collections
import itertools
import logging
import json
import math
import os
import sys

import numpy as np

# Force matplotlib to use a different backend to handle machines without a display
import matplotlib
import matplotlib.ticker as mtick
matplotlib.use('Agg')
import matplotlib.pyplot as plt  # noqa


JSON_VERSION = 20200527
MIN_X_EXP = -1
MAX_X_EXP = 2

sinames = ['', ' k', ' M', ' G', ' T']


def siname(n):
    try:
        n = float(n)
    except ValueError:
        return n

    siidx = max(0, min(len(sinames)-1,
                       int(math.floor(0 if n == 0 else math.log10(abs(n))/3)))
                )
    return '{:.0f}{}'.format(n / 10**(3 * siidx), sinames[siidx])


def init_plot(title):
    # plt.rcParams["font.family"] = "monospace"
    _, ax = plt.subplots(figsize=(9, 9))

    ax.set_xscale('log')
    ax.xaxis.set_major_formatter(mtick.FormatStrFormatter('%s'))
    ax.set_yscale('log')
    ax.yaxis.set_major_formatter(mtick.FormatStrFormatter('%s'))

    ax.grid(True, which='major')
    ax.grid(True, which='minor', linestyle='dotted', color='#DDDDDD')

    ax.set_xlabel('Slowest percentile')
    ax.set_ylabel('Response time [ms]')
    ax.set_title(title)

    return ax


def get_percentile_latency(latency_data, percentile):
    total = sum(latency_data)
    ipercentile = math.ceil((100 - percentile) / 100 * total - 1)
    assert ipercentile <= total
    i = 0
    for latency, n in enumerate(latency_data):
        i += n
        if ipercentile <= i:
            return latency
    raise RuntimeError("percentile not found")


def get_xy_from_histogram(latency_histogram):
    percentiles = np.logspace(MIN_X_EXP, MAX_X_EXP, num=200)
    y = [get_percentile_latency(latency_histogram, pctl) for pctl in percentiles]
    return percentiles, y


def merge_latency(data, since=0, until=float('+inf')):

    if type(data) is dict:
        """generate latency histogram for given period"""
        # add 100ms tolarence for interval beginning / end
        since_ms = data['stats_sum']['since_ms'] + since * 1000 - 100
        until_ms = data['stats_sum']['since_ms'] + until * 1000 + 100

        latency = []
        requests = 0
        start = None
        end = None
        for stats in data['stats_periodic']:
            if stats['since_ms'] < since_ms:
                continue
            if stats['until_ms'] >= until_ms:
                break
            requests += stats['requests']
            end = stats['until_ms']
            if not latency:
                latency = list(stats['latency'])
                start = stats['since_ms']
            else:
                assert len(stats['latency']) == len(latency)
                for i, _ in enumerate(stats['latency']):
                    latency[i] += stats['latency'][i]

        if not latency:
            raise RuntimeError('no samples matching this interval')

        qps = requests / (end - start) * 1000  # convert from ms
        return latency, qps

    else:
        maxRange = 20000
        latency = []
        for i in range(0,maxRange + 1):
            latency.append(0)
        requests = 0
        start = float('+inf')
        end = float('-inf')
        for meas in data:
            requests += 1

            begin = meas['Timestamp']['ClientUpstreamRequestTime']
            fin = meas['Timestamp']['ClientDownstreamResponseTime']
            measLatency = int((fin - begin) / 1000000)
            
            if begin < start:
                start = begin
            if begin > end:
                end = begin
            
            #if not meas['Status']:
            #    measLatency = maxRange
            if measLatency < 0:
                measLatency = maxRange
            if measLatency > maxRange:
                measLatency = maxRange
            
            #print(measLatency)
            latency[measLatency] += 1

        qps = requests / ((end - start)/1000000000)
        #print(latency)
        return latency, qps


class NamedGroupAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        if not isinstance(values, list) or len(values) <= 1:
            raise argparse.ArgumentError(
                self,
                'name required at first position, followed by one or more paths to JSON files')
        groups = getattr(namespace, self.dest) or {}
        group_name = values[0]
        try:
            groups[group_name] = [argparse.FileType()(filename) for filename in values[1:]]
        except argparse.ArgumentTypeError as ex:
            raise argparse.ArgumentError(self, ex)
        setattr(namespace, self.dest, groups)


def read_json(file_obj):
    data = json.load(file_obj)

    #try:
    #    assert data['version'] == JSON_VERSION
    #except (KeyError, AssertionError):
    #    logging.critical(
    #        "Older formats of JSON data aren't supported. "
    #        "Use older tooling or re-run the tests with newer shotgun.")
    #    sys.exit(1)

    return data


def main():
    logging.basicConfig(format='%(asctime)s %(levelname)8s  %(message)s', level=logging.DEBUG)
    logger = logging.getLogger('matplotlib')
    # set WARNING for Matplotlib
    logger.setLevel(logging.WARNING)

    parser = argparse.ArgumentParser(
        description='Plot query response time histogram from shotgun results')
    parser.add_argument('-t', '--title', default='Response Latency',
                        help='Graph title')
    parser.add_argument('-o', '--output', type=str, default='latency.svg',
                        help='output filename (default: latency.svg)')
    parser.add_argument('--since', type=float, default=0,
                        help='Omit data before this time (secs since test start)')
    parser.add_argument('--until', type=float, default=float('+inf'),
                        help='Omit data after this time (secs since test start)')

    input_args = parser.add_argument_group(
        title='input data',
        description='Shotgun result JSON file(s) to plot as individual data sets'
                    ' or groups aggregated to min/avg/max.')
    input_args.add_argument(
        '-g', '--group', nargs='+', action=NamedGroupAction,
        default={}, help='group_name json_file [json_file ...]; can be used multiple times')
    input_args.add_argument(
        'json_file', nargs='*', type=argparse.FileType(),
        help='JSON file(s) to plot individually')

    args = parser.parse_args()
    if not args.json_file and not args.group:
        parser.error('at least one input JSON file required (individually or in a group)')

    groups = collections.defaultdict(list)
    ax = init_plot(args.title)

    for json_file in args.json_file:
        logging.info('processing %s', json_file.name)
        data = read_json(json_file)
        name = os.path.splitext(os.path.basename(os.path.normpath(json_file.name)))[0]
        groups[name].append(data)

    for name, group_files in args.group.items():
        for json_file in group_files:
            logging.info('processing group %s: %s', name, json_file.name)
            data = read_json(json_file)
            groups[name].append(data)

    for name, group_data in groups.items():
        pos_inf = float('inf')
        neg_inf = float('-inf')
        group_x = []  # we use the same X coordinates for all runs
        group_ymin = []
        group_ymax = []
        group_ysum = []
        for run_data in group_data:
            latency, qps = merge_latency(run_data, args.since, args.until)
            label = '{} ({} QPS)'.format(name, siname(qps))
            group_x, run_y = get_xy_from_histogram(latency)
            if len(group_data) == 1:  # no reason to compute aggregate values
                group_ysum = run_y
                break
            group_ysum = [old + new
                          for old, new
                          in itertools.zip_longest(group_ysum, run_y, fillvalue=0)]
            group_ymin = [min(old, new)
                          for old, new
                          in itertools.zip_longest(group_ymin, run_y, fillvalue=pos_inf)]
            group_ymax = [max(old, new)
                          for old, new
                          in itertools.zip_longest(group_ymax, run_y, fillvalue=neg_inf)]
        if len(group_data) > 1:
            group_yavg = [ysum / len(group_data) for ysum in group_ysum]
            ax.fill_between(group_x, group_ymin, group_ymax, alpha=0.2)
        else:
            group_yavg = group_ysum
        ax.plot(group_x, group_yavg, lw=2, label=label)

    plt.legend()
    plt.savefig(args.output)


if __name__ == '__main__':
    main()
