import contextlib
import importlib.util
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock
import zipfile
import pandas as pd
import requests

SCRIPT = Path('outputs/COT Reports.py').resolve()
spec = importlib.util.spec_from_file_location('cot_reports', SCRIPT)
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)


def frame(rows):
    return pd.DataFrame(rows, columns=['Market_and_Exchange_Names', 'CFTC_Contract_Market_Code', 'Report_Date_as_MM_DD_YYYY'])


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.df = frame([('A','001','2028-01-04'), ('A renamed','001','2028-01-18'),
                         ('A renamed','001','2028-01-18'), ('B','002','2028-01-04'),
                         ('B','002','2028-01-11'), ('B','002','2028-01-18'),
                         ('B','002','2028-01-25'), ('New','003','2028-01-25'),
                         ('Invalid','004','bad'), ('Future','005','2029-01-01')])
    def audit(self, today='2028-01-29'):
        a, s = m.build_data_quality_audit(self.df, today=today)
        return a.set_index('CFTC_Contract_Market_Code'), s
    def test_complete_metrics_and_no_mutation(self):
        before = self.df.copy(deep=True)
        a, s = self.audit()
        r = a.loc['001']
        self.assertEqual(r['Rows'], 3)
        self.assertEqual(r['Unique_Report_Dates'], 2)
        self.assertEqual(r['Expected_Source_Dates'], 3)
        self.assertEqual(r['Coverage_Pct'], 66.67)
        self.assertEqual(r['Duplicate_Date_Rows'], 1)
        self.assertEqual(r['Long_Calendar_Gap_Count'], 1)
        self.assertEqual(r['Reports_Behind_Source'], 1)
        self.assertEqual(r['Age_Days_vs_Today'], 11)
        self.assertEqual(r['Lag_Days_vs_Source'], 7)
        self.assertEqual(r['Missing_Source_Dates'], '2028-01-11')
        self.assertEqual(r['Asset'], 'A renamed')
        self.assertIn('DUPLICATE_DATES', r['Status'])
        self.assertEqual(s['row_count'], 10)
        pd.testing.assert_frame_equal(before, self.df)
    def test_new_contract_excludes_prelisting(self):
        a, _ = self.audit()
        self.assertEqual(a.loc['003', 'Expected_Source_Dates'], 1)
        self.assertEqual(a.loc['003', 'Coverage_Pct'], 100)
        self.assertEqual(a.loc['003', 'Status'], 'CURRENT')
        self.assertEqual(a.loc['003', 'Reporting_Status'], 'REPORTED_IN_LATEST_SOURCE')
    def test_lag_distinguishes_recent_from_possibly_inactive(self):
        a, s = self.audit()
        self.assertEqual(a.loc['001', 'Reporting_Status'], 'RECENTLY_NOT_REPORTED')
        old = frame([('Old','006','2028-01-04'), ('Live','007','2028-01-04'),
                     ('Live','007','2028-01-11'), ('Live','007','2028-01-18'),
                     ('Live','007','2028-01-25')])
        result, summary = m.build_data_quality_audit(old, today='2028-01-29')
        result = result.set_index('CFTC_Contract_Market_Code')
        self.assertEqual(result.loc['006', 'Reporting_Status'], 'POSSIBLY_INACTIVE_OR_DISCONTINUED')
        self.assertEqual(result.loc['006', 'Missing_Source_Date_Count'], 0)
        self.assertEqual(summary['possibly_inactive_count'], 1)
    def test_invalid_future_remain_visible(self):
        a, s = self.audit()
        self.assertEqual(a.loc['004', 'Freshness'], 'NO_VALID_DATES')
        self.assertEqual(a.loc['005', 'Future_Date_Rows'], 1)
        self.assertEqual(s['source_latest'], '2028-01-25')
        self.assertEqual(a['Rows'].sum(), len(self.df))
    def test_stale_source_and_lag_precedence(self):
        a, s = self.audit(today='2028-02-10')
        self.assertEqual(a.loc['002', 'Freshness'], 'SOURCE_DELAYED')
        self.assertEqual(a.loc['001', 'Freshness'], 'ASSET_LAGGING')
        self.assertEqual(a.loc['001', 'Source_Status'], 'SOURCE_DELAYED')
    def test_staleness_boundary(self):
        a, _ = self.audit(today='2028-02-04')
        self.assertEqual(a.loc['002', 'Freshness'], 'CURRENT')
    def test_source_gap_not_invented_missing_date(self):
        a, s = m.build_data_quality_audit(frame([('A','1','2028-01-04'), ('A','1','2028-01-18')]), today='2028-01-20')
        self.assertEqual(a.iloc[0]['Missing_Source_Date_Count'], 0)
        self.assertEqual(len(s['source_calendar_gaps']), 1)
        self.assertNotIn('SOURCE_CALENDAR_GAPS', a.iloc[0]['Status'])
        self.assertIn('LONG_CALENDAR_GAPS', a.iloc[0]['Status'])
    def test_gap_threshold(self):
        self.assertEqual(m._find_long_gaps(pd.to_datetime(['2028-01-01','2028-01-11'])), [])
        self.assertEqual(len(m._find_long_gaps(pd.to_datetime(['2028-01-01','2028-01-12']))), 1)
    def test_empty_and_invalid_sources_fail(self):
        for f in [self.df.iloc[:0], frame([('A','1','bad')])]:
            with self.assertRaises(RuntimeError):
                m.build_data_quality_audit(f, today='2028-01-29')
    def test_mixed_dates_and_future_century(self):
        dates = m._parse_cot_dates(pd.Series(['280104', 280104.0, '20280104', '01/04/2028', pd.Timestamp('2028-01-04'), '690104', 'bad']))
        self.assertTrue(dates.iloc[:5].eq(pd.Timestamp('2028-01-04')).all())
        self.assertEqual(dates.iloc[5], pd.Timestamp('2069-01-04'))
        self.assertTrue(pd.isna(dates.iloc[6]))
    def test_column_aliases_and_date_fallback(self):
        f = self.df.iloc[:1].rename(columns={'Market_and_Exchange_Names':' MARKET AND EXCHANGE NAMES ', 'Report_Date_as_MM_DD_YYYY':'Report-Date-as-YYYY-MM-DD'})
        f.iloc[0,2] = 'bad'
        f['As_of_Date_In_Form_YYMMDD'] = '280104'
        a, _ = m.build_data_quality_audit(f, today='2028-01-08')
        self.assertEqual(a.iloc[0]['Latest_Report_Date'], '2028-01-04')
    def test_code_fallback_and_same_name_distinct_codes(self):
        f = frame([('A','1','2028-01-04'), ('A','2','2028-01-04'), ('A','','2028-01-04')])
        a, _ = m.build_data_quality_audit(f, today='2028-01-08')
        self.assertEqual(len(a), 3)
        self.assertIn('MISSING_CONTRACT_CODE', a.iloc[2]['Status'])


class DiscoveryTests(unittest.TestCase):
    def discover(self, available, now='2028-01-07', year=None, html=''):
        response = MagicMock()
        response.__enter__.return_value = response
        response.text = html
        with patch.object(m.pd, 'Timestamp') as ts, patch.object(m, '_url_exists', side_effect=lambda u: u in available), patch.object(m.HTTP, 'get', return_value=response):
            ts.now.return_value = pd.to_datetime(now)
            return m.fetch_section_report_link(m.DISAGG_SECTION_LABEL, year)
    def test_2027_2028_and_later_before_index(self):
        for year in (2027, 2028, 2035):
            url = m._fallback_urls(m.DISAGG_SECTION_LABEL, year)[0]
            self.assertEqual(self.discover({url}, now=f'{year}-01-08').year, year)
    def test_new_year_falls_back(self):
        url = m._fallback_urls(m.DISAGG_SECTION_LABEL, 2027)[0]
        self.assertEqual(self.discover({url}).year, 2027)
    def test_explicit_year_no_substitution(self):
        with self.assertRaises(RuntimeError):
            self.discover(set(), year=2027)
    def test_wrong_family_index_ignored(self):
        url = 'https://www.cftc.gov/files/dea/history/com_disagg_txt_2028.zip'
        with self.assertRaises(RuntimeError):
            self.discover({url}, year=2028, html=f'<a href="{url}">Text</a>')
    def test_year_in_filename(self):
        self.assertEqual(m._extract_year_candidates('Text', 'fut_fin_txt_2028.zip'), [2028])
    def test_probe_404_html_and_network(self):
        response = MagicMock()
        response.__enter__.return_value = response
        for status, prefix, expected in [(404,b'',False),(200,b'PK\x03\x04xxxx',True)]:
            response.status_code = status
            response.iter_content.return_value = iter([prefix])
            with patch.object(m.HTTP,'get',return_value=response):
                self.assertEqual(m._url_exists('https://example.test/a.zip'),expected)
        response.iter_content.return_value = iter([b'<html>'])
        with patch.object(m.HTTP,'get',return_value=response), self.assertRaises(RuntimeError):
            m._url_exists('https://example.test/a.zip')
        with patch.object(m.HTTP,'get',side_effect=requests.ConnectionError('offline')), self.assertRaises(requests.ConnectionError):
            m._url_exists('https://example.test/a.zip')


class IntegrationTests(unittest.TestCase):
    def test_zip_text_before_excel_skips_readme(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer,'w') as z:
            z.writestr('first.xls', b'not excel')
            z.writestr('readme.txt','This is a readme')
            z.writestr('report.txt', frame([('A','001','2028-01-04')]).to_csv(index=False))
        response = MagicMock()
        response.__enter__.return_value=response
        response.content=buffer.getvalue()
        with patch.object(m.HTTP,'get',return_value=response):
            df=m.download_report_to_dataframe('https://example.test/report.zip')
        self.assertEqual(df.iloc[0,1],'001')
if __name__ == '__main__':
    unittest.main(verbosity=2)
