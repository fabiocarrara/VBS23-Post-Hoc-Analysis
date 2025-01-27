from multiprocessing.sharedctypes import Value
from pathlib import Path
import pandas as pd
import numpy as np
import os
import tqdm
import json
import logging

from common.parsers import TeamLogParser

class TeamLogs:
    def __init__(self, data, team, max_records=10000, use_cache=False, cache_path='cache/team_logs', force=False):
        self.v3c_videos = data['v3c_videos']
        self.runreader = data['runreader']
        self.cache_path = cache_path
        self.max_records = max_records
        self.use_cache = use_cache
        self.team = team

        self.df_results, self.df_events = self._cache(data, force=force)

    def _cache(self, data, force=False):
        # some caching logic for results
        cache_path = Path(self.cache_path) # / data['version'] # append the version to the cache_path
        if not cache_path.exists():
            cache_path.mkdir(parents=True, exist_ok=True)
        results_cache_file = cache_path / '{}_results.csv'.format(self.team)
        events_cache_file = cache_path / '{}_events.csv'.format(self.team)
        if (not force and (self.use_cache and (results_cache_file.exists() and events_cache_file.exists()))) or (data['config']['logs'][self.team] is None):
            # if data['config']['logs'][self.team] is None means that the log has been preprocessed in other ways (using an external script) 
            # and we assume the .csvs are already in the correct place
            try:
                df_results = pd.read_csv(results_cache_file)
            except pd.errors.EmptyDataError:
                logging.warning(f'Empty results dataframe in {results_cache_file}')
                df_results = pd.DataFrame()

            try:
                df_events = pd.read_csv(events_cache_file)
            except pd.errors.EmptyDataError:
                logging.warning(f'Empty events dataframe in {events_cache_file}')
                df_events = pd.DataFrame()
        else:
            df_results, df_events = self.get_data(data)
            df_results.to_csv(results_cache_file, index=False)
            df_events.to_csv(events_cache_file, index=False)

        return df_results, df_events

    def _retrieve_timestamp(self, filename, js_list):
        try:
            # assume that every team has the timestamp in the filename
            timestamp = int(os.path.splitext(filename)[0])
        except ValueError:
            # try to search for a "timestamp" field in the json and return that one (e.g., vibro)
            timestamp = int(js_list['timestamp'])

        # FIXME: very bad hardcoded way to check if it is milliseconds or seconds
        if timestamp < 1e11:
            timestamp = timestamp * 1000
            
        return timestamp

    def get_data(self, data):
        """
        retrieve all the data
        """
        skipped_log_inTask_info=[]
        results_dfs = []
        events_dfs = []
        team = self.team
        team_log = data['config']['logs'][team]

        if team_log is None:
            logging.info('Log for {} is None. Forcing the use of the cache'.format(team))
            return self._cache(data, force=True)

        user_idx = 0
        log_parser = TeamLogParser(data, team, self.v3c_videos)
        for root, _, files in os.walk(team_log):
            for file in tqdm.tqdm(files, desc="Processing {} logs".format(team)):
                path = os.path.join(root, file)
                if file == '.DS_Store':
                    continue
                with open(path) as f:
                    ranked_list = json.load(f)

                    timestamp = self._retrieve_timestamp(file, ranked_list)                    

                    # retrieve the task we are in at the moment
                    task = self.runreader.tasks.get_task_from_timestamp(timestamp)
                    if task is None:
                        # the logs outside task ranges are not important for us
                        continue
                    task_name = task['name']

                    # if a team already submitted, all the subsequent logs are just noise, delete them
                    csts = self.runreader.get_csts()
                    cst = csts[team][task_name]
                    if cst > 0 and timestamp > cst:
                        skipped_log_inTask_info.append(f"{timestamp}, {cst}, {timestamp-cst}, {task_name}")
                        continue

                    # grab relevant infos from different team log files
                    if len(ranked_list.get('results',[])) > 0:
                        results_df = self.get_teams_results(ranked_list['results'], log_parser.get_results, self.max_records)

                        results_df['timestamp'] = timestamp
                        results_df['user'] = user_idx
                        results_df['task'] = task_name
                        results_df['team'] = team
                        results_dfs.append(results_df)

                    if len(ranked_list.get('events',[])) > 0:
                        events_df = self.get_teams_events(ranked_list['events'], log_parser.get_events)
                    else:
                        # FIXME: remove this part and handle VERGE problems differently
                        events_df = pd.DataFrame({'category': [None], 'type': [None], 'value': [None], 'additionals': [None]})
                        logging.warning('Empty or non-existing events in the log!')
                    # note that in events the timestamp should be already present, but in some logs it is approximated (e.g., verge)
                    # so it is better to get it directly from the file (otherwise the match using the timestamp does not work)
                    events_df['timestamp'] = timestamp
                    events_df['elapsed_since_task_start_ms'] = timestamp - task['started']
                    events_df['correct_submission_time_ms'] = cst - task['started'] if cst > 0 else np.nan

                    events_df['user'] = user_idx
                    events_df['task'] = task_name
                    events_df['team'] = team
                    events_dfs.append(events_df)

            if Path(root) != Path(team_log):
                user_idx += 1   # number of user is the number of folders

        if len(skipped_log_inTask_info) > 0:
            print(f"**{team}**")
            print(f" log_timestamp, correct_submission_timestamp, timestamp - cst,task_name")
            print("\n".join(skipped_log_inTask_info))
        assert user_idx <= 2

        # prepare the final dataframe
        results_df = pd.concat(results_dfs, axis=0).reset_index(drop=True)
        events_df = pd.concat(events_dfs, axis=0).reset_index(drop=True)

        # for each timestamp, find the ranks of correct results
        ranks_df = results_df.groupby('timestamp').apply(lambda x: self.get_rank_of_correct_results(x))
        ranks_df = ranks_df.reset_index()
        # merge this table with the events, the key is the timestamp column
        events_and_ranks_df = events_df.merge(ranks_df, on='timestamp')
        # sometimes there are duplicated entries due to log repetitions. Remove them
        events_and_ranks_df.drop_duplicates(inplace=True)
        # reordering columns
        events_and_ranks_df= events_and_ranks_df[['task', 'team', 'user', 'timestamp', 'elapsed_since_task_start_ms', 'correct_submission_time_ms', 
                            'rank_video', 'rank_shot_margin_0', 'rank_shot_margin_5', 'max_rank', 'category', 'type', 'value', 'additionals']]

        return results_df, events_and_ranks_df

    def get_teams_events(self, events, events_fn):
        if isinstance(events, dict):
            events = [events]
        events = pd.DataFrame(events)
        standard_attrs = events_fn(events)

        # collect non-standard attributes of the event in a single column "additionals"
        non_standard_attrs = list(set(events.columns) - set(standard_attrs.columns))
        standard_attrs['additionals'] = events[non_standard_attrs].apply(lambda x: x.to_json(), axis=1)
        return standard_attrs

    def get_teams_results(self, results, results_fn, max_records):
        results = results[:max_records]
        results = pd.DataFrame(results)
        results = results_fn(results)

        # correct if rank is zero-based
        min_rank = results['rank'].min()
        assert min_rank in [0, 1]
        if min_rank == 0:
            results['rank'] = results['rank'] + 1

        return results

    def get_rank_of_correct_results(self, result, method='timeinterval', target_shot_margins=[0, 5]): # 'shotid' or 'timeinterval'
        """
        computes the rank of correct video or shot
        method: "shotid" or "timeinterval". "timeinterval" is the default for the 2022 evaluation (uses target shot boundaries and not the shot id)
        target_shot_margins [list]: temporal margins of expansion of the temporal window of the target video, expressed in seconds. Only used if method=='timeinterval'
        """
        # best_logged_rank_video = float('inf')
        # best_logged_rank_shot = float('inf')
        assert not result.empty
        task_name = result['task'].iloc[0]
        task = self.runreader.tasks.get_task_from_taskname(task_name)
        res = result

        # initialize result dictionary
        results = {'rank_video': float('inf')}
        if method == 'timeinterval':
            results.update({'rank_shot_margin_{}'.format(m): float('inf') for m in target_shot_margins})
        else:
            results.update({'rank_shot': float('inf')})

        results['max_rank'] = max(result['rank'])   # FIXME

        # find correct videos
        res = res[res['videoId'] == task['correct_video']]
        
        if not res.empty:
            best_video_rank_idx = res[['rank']].idxmin().iat[0]
            results['rank_video'] = res[['rank']].at[best_video_rank_idx, 'rank']
            # best_logged_time_video = res[['adj_logged_time']].at[best_video_rank_idx, 'adj_logged_time']

            if method == 'shotid':
                # use shot id to discriminate the correct results
                res = res[res['shotId'] == task['correct_shot']]
                # check also for best shot rank
                if not res.empty:
                    best_shot_rank_idx = res[['rank']].idxmin().iat[0]
                    results['rank_shot'] = res[['rank']].at[best_shot_rank_idx, 'rank']

            elif method == 'timeinterval':
                for margin in target_shot_margins:
                    # use the time interval of the shot target to discriminate the correct results
                    res_margin = res[res['shotTimeMs'].between(task['target_start_ms'] - (margin * 1000), task['target_end_ms'] + (margin * 1000))]
                    if not res_margin.empty:
                        best_shot_rank_idx = res_margin[['rank']].idxmin().iat[0]
                        results['rank_shot_margin_{}'.format(margin)] = res_margin[['rank']].at[best_shot_rank_idx, 'rank']

        return pd.Series(results)

    def filter_by_timestep(self, start_timestep, end_timestep):
        # easy but expensive solution
        t1 = self.df_results[self.df_results['timestamp'].between(start_timestep, end_timestep)].copy()
        
        # efficient implementation using bisect
        # timestamps = self.df['timestamp'].to_list()
        # start_idx = bisect.bisect_right(timestamps, start_timestep)
        # end_idx = bisect.bisect_right(timestamps, end_timestep)
        # t2 = self.df.iloc[start_idx:end_idx].copy()
        return t1

    def filter_by_task_name(self, task_name):
        t1 = self.df_results[self.df_results['task'] == task_name].copy()
        return t1

    def get_events_dataframe(self):
        return self.df_events

    def get_raw_results_dataframe(self):
        return self.df_results
