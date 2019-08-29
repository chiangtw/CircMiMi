import os
import shutil
import subprocess as sp
import tempfile as tp
import random
import re
import pandas as pd
from itertools import cycle
from functools import reduce
from contextlib import contextmanager
from circmimi.seq import Seq


class Miranda:
    RESULT_TITLE = (
        'query_id',
        'reference_id',
        'score',
        'energy',
        'query_start',
        'query_end',
        'ref_start',
        'ref_end',
        'aln_length',
        'identity',
        'similarity',
        'aln_mirna',
        'aln_map',
        'aln_utr'
    )

    def __init__(self, ref_file, work_dir='.',
                 bin_path='miranda', options=None):
        self.ref_file = ref_file
        self.work_dir = work_dir
        self.bin_path = bin_path

        if options:
            self.options = options
        else:
            self.options = []
        self.options += ['-keyval']

    def _generate_cmd(self, seq_file, out_file=None):
        cmd = [self.bin_path, self.ref_file, seq_file] + self.options

        if out_file:
            cmd += ['-out', out_file]

        return cmd

    @staticmethod
    def _rand_id(n=5):
        return '{{:0>{}}}'.format(n).format(random.randrange(10**n))

    @contextmanager
    def _split_file(self, seq_file, num):
        tmp_dir = os.path.join(
            self.work_dir,
            'miranda_tmp_{}'.format(self._rand_id())
        )
        os.makedirs(tmp_dir)

        filenames = ['seq_file.part{}.fa'.format(i) for i in range(1, num + 1)]
        file_paths = [os.path.join(tmp_dir, fname) for fname in filenames]
        tmp_files = [open(path, 'w') for path in file_paths]

        tmp_files_iter = cycle(tmp_files)
        with open(seq_file) as seq_in:
            for line in seq_in:
                if line.startswith('>'):
                    tmp_f = next(tmp_files_iter)
                tmp_f.write(line)

        for tmp_f in tmp_files:
            tmp_f.close()

        try:
            yield file_paths
        finally:
            shutil.rmtree(tmp_dir)

    def run(self, seq_file, num_proc=1):
        if num_proc == 1:
            cmd = self._generate_cmd(seq_file)
            res = sp.run(cmd, stdout=sp.PIPE, encoding='utf-8')
            return res.stdout
        elif num_proc > 1:
            with self._split_file(seq_file, num=num_proc) as tmp_files:
                out_files = ["{}.result".format(tmp_f) for tmp_f in tmp_files]

                cmds = [self._generate_cmd(tmp_f, out_f)
                        for tmp_f, out_f in zip(tmp_files, out_files)]
                procs = [sp.Popen(cmd) for cmd in cmds]

                for p in procs:
                    p.wait()

                result = ''
                for out_f in out_files:
                    with open(out_f) as res_in:
                        result += res_in.read()

            return result

    @staticmethod
    def _get_value(res_line):
        return [kv.split('=')[1] for kv in res_line.split('\t')]

    @classmethod
    def parse_result(cls, raw_result):
        aln_res_pat = re.compile(r'^//hit_info\t(.*)', flags=re.M)
        for m in re.finditer(aln_res_pat, raw_result):
            yield cls._get_value(m.group(1))


def get_binding_sites(seq_df, mir_ref_file, num_proc=1):
    miranda = Miranda(mir_ref_file, options=['-quiet'])

    with tp.NamedTemporaryFile(dir='.') as tmp_fa_file:
        with open(tmp_fa_file.name, 'w') as fa_out:
            fa_txt = Seq.to_fasta(seq_df)
            fa_out.write(fa_txt)

        raw_result = miranda.run(tmp_fa_file.name, num_proc=num_proc)
        miranda_df = pd.DataFrame(
            Miranda.parse_result(raw_result),
            columns=Miranda.RESULT_TITLE
        ).astype({
            'score': 'float',
            'energy': 'float',
            'query_start': 'int',
            'query_end': 'int',
            'ref_start': 'int',
            'ref_end': 'int',
            'aln_length': 'int',
            'identity': 'float',
            'similarity': 'float'
        })

        return miranda_df


class MirandaUtils:
    @staticmethod
    def append_exons_len(miranda_df, exons_len_df):
        miranda_df_with_total_len = miranda_df.merge(
            exons_len_df,
            left_on='reference_id',
            right_on='exons_id',
            how='left'
        ).drop('exons_id', axis=1)

        return miranda_df_with_total_len

    @staticmethod
    def _get_extended_ref_end(s):
        ext_ref_end = s['ref_end'] + s['total_len']
        ext_s = pd.concat([
            s[['query_id', 'reference_id']],
            pd.Series([ext_ref_end], index=['ext_ref_end'])
        ])
        return ext_s

    @classmethod
    def remove_redundant_result(cls, miranda_df):
        filtered_df = miranda_df[
            miranda_df['ref_start'] <= miranda_df['total_len']
        ]

        dropped_index = filtered_df[filtered_df['ref_start'] == 1]\
            .apply(cls._get_extended_ref_end, axis=1)\
            .reset_index()\
            .merge(filtered_df,
                   left_on=['query_id', 'reference_id', 'ext_ref_end'],
                   right_on=['query_id', 'reference_id', 'ref_end'],
                   how='inner')\
            .loc[:, 'index']

        filtered_df = filtered_df.drop(dropped_index).reset_index(drop=True)

        return filtered_df

    @staticmethod
    def _is_cross_boundary(s):
        exons_len = s['total_len']
        ref_start = s['ref_start']
        ref_end = s['ref_end']

        if (ref_start <= exons_len) and (exons_len < ref_end):
            return 1
        else:
            return 0

    @classmethod
    def append_cross_boundary(cls, miranda_df_with_len):
        cross_boundary_df = pd.DataFrame(
            miranda_df_with_len.apply(cls._is_cross_boundary, axis=1),
            columns=['cross_boundary']
        )
        df_with_cross_boundary = pd.concat(# TODO: replace 'concat' by 'assign'
            [
                miranda_df_with_len,
                cross_boundary_df
            ],
            axis=1
        )

        return df_with_cross_boundary

    @staticmethod
    def append_ev_id(df, exons_ev_id_df):
        df_with_ev_id = df.merge(
            exons_ev_id_df,
            left_on='reference_id',
            right_on='exons_id',
            how='left'
        ).drop(
            columns='exons_id'
        ).sort_values(
            ['ev_id', 'query_id', 'score'],
            ascending=[True, True, False]
        )

        return df_with_ev_id

    @staticmethod
    def append_merged_aln(miranda_df):
        merged_aln = reduce(
            lambda c1, c2: c1.add(c2),
            map(miranda_df.get, ['aln_mirna', 'aln_map', 'aln_utr'])
        )
        miranda_df_with_merged_aln = miranda_df.assign(aln=merged_aln)

        return miranda_df_with_merged_aln

    @classmethod
    def generate_aln_id(cls, miranda_df_with_aln):
        all_uniq_alns = pd.Series(
            miranda_df_with_aln['aln'].unique(),
            name='aln'
        )
        miranda_df_with_aln_id = miranda_df_with_aln.merge(
            all_uniq_alns.reset_index(),
            on='aln',
            how='left'
        ).rename({'index': 'aln_id'}, axis=1)

        return miranda_df_with_aln_id

    @staticmethod
    def get_grouped_results(miranda_df_with_aln_id):
        grouped_res_df = miranda_df_with_aln_id[[
            'ev_id',
            'query_id',
            'score',
            'aln_id',
            'cross_boundary'
        ]].groupby([
            'ev_id',
            'query_id'
        ]).agg({
            'score': 'max',
            'aln_id': 'nunique',
            'cross_boundary': 'max'
        }).reset_index().rename(
            {
                'query_id': 'mirna',
                'score': 'max_score',
                'aln_id': 'count'
            },
            axis=1
        )

        return grouped_res_df
