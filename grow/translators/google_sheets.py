from . import base
from babel.messages import catalog
from babel.messages import pofile
from googleapiclient import discovery
from googleapiclient import errors
from googleapiclient import http
from grow.common import oauth
from grow.common import utils
from grow.preprocessors import google_drive
import base64
import csv
import datetime
import httplib2
import json
import logging
import urllib
try:
    import cStringIO as StringIO
except ImportError:
    try:
        import StringIO
    except ImportError:
        from io import StringIO


class AccessLevel(object):
    ADMIN = 'ADMIN'
    READ_AND_COMMENT = 'READ_AND_COMMENT'
    READ_AND_WRITE = 'READ_AND_WRITE'
    READ_ONLY = 'READ_ONLY'


class GoogleSheetsTranslator(base.Translator):
    KIND = 'google_sheets'
    has_immutable_translation_resources = False
    has_multiple_langs_in_one_resource = True

    def _create_service(self):
        return google_drive.BaseGooglePreprocessor.create_service('sheets', 'v4')

    def _download_sheet(self, spreadsheet_id, locale):
        service = self._create_service()
        rangeName = "'{}'!A:B".format(stat.lang)
        resp = service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id, range=rangeName).execute()
        return resp['values']

    def _download_content(self, stat):
        spreadsheet_id = stat.ident
        values = self._download_sheet(spreadsheet_id, stat.lang)
        source_lang, lang = values.pop(0)
        babel_catalog = catalog.Catalog(stat.lang)
        for row in values:
            source = row[0]
            translation = row[1] if len(row) > 1 else None
            babel_catalog.add(source, translation, auto_comments=[],
                              context=None, flags=[])
        updated_stat = base.TranslatorStat(
            url=stat.url,
            lang=stat.lang,
            downloaded = datetime.datetime.now(),
            source_lang=stat.source_lang,
            ident=stat.ident)
        fp = StringIO.StringIO()
        pofile.write_po(fp, babel_catalog)
        fp.seek(0)
        content = fp.read()
        return updated_stat, content

    def _upload_catalogs(self, catalogs, source_lang):
        project_title = self.project_title
        source_lang = str(source_lang)

        # Get existing sheet ID (if it exists) from one stat.
        spreadsheet_id = None
        stats_to_download = self._get_stats_to_download([])
        if stats_to_download:
            stat = stats_to_download.values()[0]
            spreadsheet_id = stat.ident if stat else None

        # NOTE: Manging locales across multiple spreadsheets is unsupported.
        service = self._create_service()
        if spreadsheet_id:
            resp = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
            locales_to_sheet_ids = {}
            for sheet in resp['sheets']:
                locales_to_sheet_ids[sheet['properties']['title']] = sheet['properties']['sheetId']
            catalogs_to_create = []
            catalogs_to_update = []
            for catalog in catalogs:
                existing_sheet_id = locales_to_sheet_ids.get(str(catalog.locale))
                if existing_sheet_id:
                    catalogs_to_update.append(catalog)
                else:
                    catalogs_to_create.append(catalog)
            self._create_sheets_request(catalogs_to_create, source_lang, spreadsheet_id)
            self._update_sheets_request(catalogs_to_update, source_lang, spreadsheet_id)
        else:
            sheets = []
            for catalog in catalogs:
                sheets.append(self._create_sheet_from_catalog(catalog, source_lang))
            resp = service.spreadsheets().create(body={
                'sheets': sheets,
                'properties': {
                    'title': project_title,
                },
            }).execute()
            spreadsheet_id = resp['spreadsheetId']

        url = 'https://docs.google.com/spreadsheets/d/{}'.format(spreadsheet_id)
        stats = []
        for catalog in catalogs:
            stat = base.TranslatorStat(
                url=url,
                lang=str(catalog.locale),
                source_lang=source_lang,
                uploaded = datetime.datetime.now(),
                ident=spreadsheet_id)
            stats.append(stat)
        return stats

    def _create_header_row_data(self, source_lang, lang):
        return {
            'values': [
                {
                    'userEnteredValue': {'stringValue': source_lang},
                    'userEnteredFormat': {
                        'backgroundColor': {
                            'red': 50,
                            'blue': 50,
                            'green': 50,
                            'alpha': .1,
                        },
                        'textFormat': {'bold': True},
                    }
                },
                {
                    'userEnteredValue': {'stringValue': lang},
                    'userEnteredFormat': {
                        'backgroundColor': {
                            'red': 50,
                            'blue': 50,
                            'green': 50,
                            'alpha': .1,
                        },
                        'textFormat': {'bold': True},
                    }
                },
            ]
        }

    def _create_catalog_rows(self, catalog):
        rows = []
        for message in catalog:
            if not message.id:
                continue
            rows.append({
                'values': [
                    {
                        'userEnteredValue': {'stringValue': message.id},
                        'userEnteredFormat': {'wrapStrategy': 'WRAP'}
                    },
                    {
                        'userEnteredValue': {'stringValue': message.string},
                        'userEnteredFormat': {'wrapStrategy': 'WRAP'}
                    },
                ],
            })
        return rows

    def _create_sheet_from_catalog(self, catalog, source_lang):
        lang = str(catalog.locale)
        row_data = []
        row_data.append(self._create_header_row_data(source_lang, lang))
        row_data += self._create_catalog_rows(catalog)
        return {
            'properties': {
                'title': lang,
                'gridProperties': {
                    'columnCount': 2,
                    'rowCount': len(row_data) + 3,  # Three rows of padding.
                    'frozenRowCount': 1,
                    'frozenColumnCount': 1,
                },
            },
            'data': [{
                'startRow': 0,
                'startColumn': 0,
                'rowData': row_data,
                'columnMetadata': [
                    {'pixelSize': 400},
                    {'pixelSize': 400},
                ],
            }],
        }

    def _create_sheets_request(self, catalogs, source_lang, spreadsheet_id):
        service = self._create_service()

        # Create sheets.
        requests = []
        for catalog in catalogs:
            sheet = self._create_sheet_from_catalog(catalog, source_lang)
            request = {
                'addSheet': {
                    'properties': sheet['properties']
                },
            }
            requests.append(request)
        body = {'requests': requests}
        resp = service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id, body=body).execute()

        # Format newly-created sheets.
        requests = []
        for reply in resp['replies']:
            sheet_id = reply['addSheet']['properties']['sheetId']
            requests.append({
                'updateCells': {
                    'fields': '*',
                    'start': {
                        'sheetId': sheet_id,
                        'rowIndex': 0,
                        'columnIndex': 0,
                    },
                    'rows': sheet['data'][0]['rowData']
                },
            })
            requests.append({
                'updateDimensionProperties': {
                    'range': {
                        'sheetId': sheet_id,
                        'dimension': 'COLUMNS',
                    },
                    'properties': {
                        'pixelSize': 400,
                    },
                    'fields': '*',
                },
            })
        body = {'requests': requests}
        resp = service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id, body=body).execute()

    def _update_sheets_request(self, catalogs, source_lang, spreadsheet_id):
        pass
#        service = self._create_service()
#        values = self._download_sheet(spreadsheet_id, stat.lang)
#        source_lang, lang = values.pop(0)
