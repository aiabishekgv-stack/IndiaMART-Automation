/*
 * NUNES IndiaMART Automation V2 - Google Sheet Bridge
 *
 * Setup:
 * 1. Open the Google Sheet -> Extensions -> Apps Script.
 * 2. Paste this file.
 * 3. Change TOKEN below to a long random value.
 * 4. Deploy as Web App, Execute as Me, access to users with the link.
 * 5. Put the /exec URL and TOKEN into the V2 .env file.
 */

const TOKEN = 'CHANGE_THIS_TO_A_LONG_RANDOM_TOKEN';
const SOURCE_SHEET = 'final sheet';
const LOG_SHEET = 'IndiaMART Upload Log';

function json_(obj) {
  return ContentService
    .createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

function auth_(token) {
  return String(token || '') === TOKEN && TOKEN !== 'CHANGE_THIS_TO_A_LONG_RANDOM_TOKEN';
}

function headerIndex_(headers, names) {
  const norm = headers.map(x => String(x || '').trim().toLowerCase());
  for (const name of names) {
    const i = norm.indexOf(String(name).toLowerCase());
    if (i >= 0) return i;
  }
  return -1;
}

function ensureColumn_(sheet, header) {
  const lastCol = Math.max(sheet.getLastColumn(), 1);
  const headers = sheet.getRange(1, 1, 1, lastCol).getValues()[0];
  let idx = headerIndex_(headers, [header]);
  if (idx >= 0) return idx + 1;
  const newCol = lastCol + 1;
  sheet.getRange(1, newCol).setValue(header);
  return newCol;
}

function rowObject_(headers, values) {
  const obj = {};
  headers.forEach((h, i) => {
    const key = String(h || '').trim();
    if (key) obj[key] = values[i];
  });
  return obj;
}

function doGet(e) {
  try {
    const p = e && e.parameter ? e.parameter : {};
    if (!auth_(p.token)) return json_({ok:false, error:'Invalid token'});
    const action = String(p.action || 'ping').toLowerCase();
    if (action === 'ping') return json_({ok:true, service:'NUNES_V2_SHEET_BRIDGE'});
    if (action !== 'next') return json_({ok:false, error:'Unknown action'});

    const ss = SpreadsheetApp.getActiveSpreadsheet();
    const sheet = ss.getSheetByName(SOURCE_SHEET);
    if (!sheet) return json_({ok:false, error:'Source sheet not found: ' + SOURCE_SHEET});

    const lastRow = sheet.getLastRow();
    const lastCol = sheet.getLastColumn();
    if (lastRow < 2 || lastCol < 1) return json_({ok:true, empty:true, message:'Source sheet has no data rows.'});

    const headers = sheet.getRange(1,1,1,lastCol).getValues()[0];
    const productIdx = headerIndex_(headers, ['Product Name','Product','Keyword','Search Keyword']);
    if (productIdx < 0) return json_({ok:false, error:'Product Name column not found.'});

    const statusCol = ensureColumn_(sheet, 'IndiaMART Status');
    const effectiveLastCol = sheet.getLastColumn();
    const effectiveHeaders = sheet.getRange(1,1,1,effectiveLastCol).getValues()[0];
    const statusIdx = statusCol - 1;
    const values = sheet.getRange(2,1,lastRow-1,effectiveLastCol).getValues();

    for (let i=0; i<values.length; i++) {
      const product = String(values[i][productIdx] || '').trim();
      const status = String(values[i][statusIdx] || '').trim().toUpperCase();
      if (!product) continue;
      if (status && status !== 'RETRY' && status !== 'FAILED') continue;
      return json_({
        ok:true,
        empty:false,
        source_row:i+2,
        row:rowObject_(effectiveHeaders, values[i])
      });
    }
    return json_({ok:true, empty:true, message:'No unprocessed product rows found.'});
  } catch (err) {
    return json_({ok:false, error:String(err && err.message ? err.message : err)});
  }
}

function doPost(e) {
  try {
    const body = JSON.parse((e && e.postData && e.postData.contents) || '{}');
    if (!auth_(body.token)) return json_({ok:false, error:'Invalid token'});
    if (String(body.action || '').toLowerCase() !== 'log') return json_({ok:false, error:'Unknown action'});
    const data = body.data || {};

    const ss = SpreadsheetApp.getActiveSpreadsheet();
    let log = ss.getSheetByName(LOG_SHEET);
    if (!log) {
      log = ss.insertSheet(LOG_SHEET);
      log.appendRow(['Timestamp','Source Row','Product Name','Model','Status','Decision','IndiaMART URL','Message']);
    }
    log.appendRow([
      new Date(), data.source_row || '', data.product_name || '', data.model || '',
      data.status || '', data.decision || '', data.url || '', data.message || ''
    ]);

    const source = ss.getSheetByName(SOURCE_SHEET);
    const row = Number(data.source_row || 0);
    if (source && row >= 2) {
      const statusCol = ensureColumn_(source, 'IndiaMART Status');
      const decisionCol = ensureColumn_(source, 'IndiaMART Decision');
      const urlCol = ensureColumn_(source, 'IndiaMART URL');
      const updatedCol = ensureColumn_(source, 'IndiaMART Updated At');
      source.getRange(row, statusCol).setValue(data.status || 'DONE');
      source.getRange(row, decisionCol).setValue(data.decision || '');
      source.getRange(row, urlCol).setValue(data.url || '');
      source.getRange(row, updatedCol).setValue(new Date());
    }
    return json_({ok:true});
  } catch (err) {
    return json_({ok:false, error:String(err && err.message ? err.message : err)});
  }
}
