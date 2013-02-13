from __future__ import print_function, division

from collections import defaultdict
import logging
import time

import numpy as np
import pandas as pd

from common import TO_BE_PLACED, pandas_to_dicts
from analytics import HorseModel
import risk

WARN_LIQUIDITY = 0.2
DEFAULT_COMM = 0.95


class Strategy(object):
    def __init__(self, db, vwao='vwao', train='train'):
        self.vwao_coll = vwao
        self.train_coll = train
        self.db = db
        self._bets = []

    def bet(self, event_id, selection, amount, user_fields=None):
        assert event_id == self._curr['event_id']
        if user_fields is None:
            user_fields = {}

        odds = self.vwao.get_value((event_id, selection), 'vwao')
        win = int(selection in self._curr['winners'])
        pnl = win * amount * (odds - 1) - (1 - win) * amount

        bet = {'event_id': event_id,
               'scheduled_off': self._curr['scheduled_off'],
               'selection': selection,
               'amount': amount,
               'odds': odds,
               'pnl': pnl,
               'win': self._curr['winners']}
        bet.update(map(lambda x: ('user_' + x[0], x[1]), user_fields.items()))
        self._bets.append(bet)

    def handle_race(self, race):
        raise RuntimeError('Abstract base class: implement the function')

    def run(self, country=None, start_date=None, end_date=None):
        where_clause = defaultdict(lambda: {})
        if start_date is not None:
            where_clause['scheduled_off']['$gte'] = start_date
        if end_date is not None:
            where_clause['scheduled_off']['$lte'] = end_date

        self.vwao = pd.DataFrame(list(db[self.vwao_coll].find(where_clause, sort=[('scheduled_off', 1)])))
        self._total_matched = self.vwao.groupby('event_id')['volume_matched'].sum()
        self.vwao = self.vwao.set_index(['event_id', 'selection'])

        if country is not None:
            where_clause['country'] = country
        races = self.db[self.train_coll].find(where_clause, sort=[('scheduled_off', 1)])
        logging.info('Running strategy on %d historical races [coll=%s, start_date=%s, end_date=%s].' %
                     (races.count(), self.db[self.train_coll], start_date, end_date))

        start_time = time.clock()
        for i, race in enumerate(races):
            self._curr = race
            self.handle_race(race)
            if i > 0 and i % 100 == 0:
                pnl = sum(map(lambda x: x['pnl'] if np.isfinite(x['pnl']) else 0.0, self._bets))
                logging.info('%s races backtested so far [last 100 took %.2fs; n_bets = %d; pnl = %.2f]'
                             % (i, time.clock() - start_time, len(self._bets), pnl))
                start_time = time.clock()

    def get_total_matched(self, event_id):
        return self._total_matched.get_value(event_id)

    def get_bets(self):
        return self._bets


def make_scorecard(strategy, percentile_width=60, comm=DEFAULT_COMM):
    def calculate_collateral(group):
        return np.min(risk.nwin1_bet_returns(group.amount.values, group.odds.values))

    bets_summary = ['amount', 'pnl', 'odds']
    bets = pd.DataFrame.from_dict(strategy.get_bets())

    events = pd.DataFrame.from_dict([{'event_id': k,
                                      'pnl_gross': v.pnl.sum(),
                                      'coll': calculate_collateral(v),
                                      'scheduled_off': v['scheduled_off'].iget(0)}
                                    for k, v in bets.groupby('event_id')]).set_index('event_id')
    events['pnl_net'] = events.pnl_gross
    events['pnl_net'][events.pnl_net > 0] *= comm

    daily_pnl = events[['scheduled_off', 'pnl_gross', 'pnl_net']]
    daily_pnl['scheduled_off'] = daily_pnl['scheduled_off'].map(lambda t: datetime.datetime(t.year, t.month, t.day))
    daily_pnl = daily_pnl.groupby('scheduled_off').sum().rename({'pnl_gross': 'gross', 'pnl_net': 'net'})
    daily_pnl['gross_cumm'] = daily_pnl['gross'].cumsum()
    daily_pnl['bet_cumm'] = daily_pnl['net'].cumsum()

    scorecard = {
        'all': bets[bets_summary].describe(percentile_width).to_dict(),
        'backs': bets[bets['amount'] > 0][bets_summary].describe(percentile_width).to_dict(),
        'lays': bets[bets['amount'] < 0][bets_summary].describe(percentile_width).to_dict(),
        'events': events.describe(percentile_width).to_dict(),
        'pnl': pandas_to_dicts(events[['gross', 'net']])
    }

    return scorecard, events


class Jockey(Strategy):
    def __init__(self, db, vwao='vwao', train='train'):
        super(Jockey, self).__init__(db, vwao, train)
        self.hm = HorseModel()

    def handle_race2(self, race):
        if race['event'] != TO_BE_PLACED:
            return
        #logging.info('To be placed event (event_id = %d)' % race['event_id'])
        try:
            vwao = self.vwao.ix[race['event_id']]['vwao']
            self.bet(race['event_id'], vwao[vwao == vwao.min()].index[0], 2.0)
        except KeyError:
            logging.warn('No VWAO for %d' % race['event_id'])

    def handle_race(self, race):
        if race['event'] == TO_BE_PLACED or race['n_runners'] < 3:
            return

        runners = race['selection']
        # self.total_matched(race['event_id']) > 2e5
        if np.all(self.hm.get_runs(runners) > 2):
            vwao = self.vwao.ix[race['event_id']]['vwao'][runners].values
            #q = 1.0 / vwao / np.sum(1.0 / vwao)
            q = 1.0 / vwao
            p = self.hm.pwin_trapz(runners)

            rel = p / q - 1.0
            t = 0.1

            p[rel < -t] = q[rel < -t] * 0.9
            p[rel > t] = q[rel > t] * 1.1

            #print(p)
            # ps = (self.hm.get_runs(runners) * p + 4 * q) / (4 + self.hm.get_runs(runners))

            #w = RiskModel2(p, q).optimal_w()
            w = risk.nwin1_l2reg(p, q, 0.1)

            coll = np.min(risk.nwin1_bet_returns(w, 1 / q))
            if coll > -60:
                logging.info('Placing some bets: %.2f' % np.sum(np.abs(w)))
                [self.bet(race['event_id'], r, w[i], {'p': p[i]}) for i, r in enumerate(runners)]
            else:
                logging.info('Skipping placing bets as coll=%.2f' % coll)

        self.hm.fit_race(race)

#        logging.info('To be placed event (event_id = %d)' % race['event_id'])
#        vwao = self.vwao.ix[race['event_id']]['vwao']
#        self.bet(race['event_id'], vwao[vwao == vwao.min()].index[0], 2.0)


if __name__ == '__main__':
    import datetime
    from pymongo import MongoClient
    from common import configure_root_logger

    configure_root_logger(True)

    db = MongoClient(port=30001)['betfair']
    algo = Jockey(db)

    st = time.clock()
    algo.run('GB', datetime.datetime(2012, 1, 1), datetime.datetime(2013, 1, 1))
    en = time.clock()

    df = pd.DataFrame.from_dict(algo._bets)
    print(df.to_string())
    print('Done in %.4f s' % (en - st))

    df.save('/home/marius/playground/btrading/back5.pd')

