"""Portfolio editing and preview persistence acceptance tests."""
from datetime import datetime, timezone
from decimal import Decimal
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from portfolio_analyzer.imports import validate_portfolio_snapshot
from portfolio_analyzer.models import PortfolioSnapshot, Position
from portfolio_analyzer.web import create_app


def test_editor_normalizes_cash_and_preserves_missing_prices():
    snapshot = PortfolioSnapshot(as_of=datetime.now(timezone.utc), cash='5', positions=[
        Position(symbol='ABC', quantity='2'), Position(symbol='CASH', quantity='20'),
        Position(symbol='USD', quantity='3', price='2'),
        Position(symbol='DOLLARS', quantity='4', asset_type=' Cash '),
    ])
    saved = validate_portfolio_snapshot(snapshot)
    assert saved.cash == Decimal('35')
    assert [p.symbol for p in saved.positions] == ['ABC']
    assert saved.positions[0].price is None
    assert len(snapshot.positions) == 4


def test_editor_rejects_duplicate_holdings():
    snapshot = PortfolioSnapshot(as_of=datetime.now(timezone.utc), positions=[
        Position(symbol='ABC', quantity=1), Position(symbol='abc', quantity=2)])
    with pytest.raises(ValueError, match='Duplicate'):
        validate_portfolio_snapshot(snapshot)


def test_portfolio_preview_edit_save_reload(tmp_path):
    with TestClient(create_app(tmp_path), base_url='http://127.0.0.1:8000') as client:
        page = client.get('/')
        token = re.search(r'name="csrf-token" content="([^"]+)"', page.text).group(1)
        headers = {'x-csrf-token': token}
        assert 'id="portfolio-form"' in page.text
        preview = client.post('/api/import', headers=headers, json={
            'csv': 'symbol,quantity,price\nABC,2,\nCASH,"$1,000",',
            'as_of': '2026-10-18T12:00:00Z'})
        assert preview.status_code == 200, preview.text
        assert client.get('/api/status').json()['snapshot'] is None
        draft = preview.json()
        draft['cash'] = '1250.50'
        draft['positions'][0]['quantity'] = '4'
        draft['positions'].append({'symbol': 'XYZ', 'quantity': '3', 'price': '5'})
        saved = client.post('/api/portfolio', headers=headers, json={'snapshot': draft})
        assert saved.status_code == 200, saved.text
        result = client.get('/api/status').json()['snapshot']
        assert result['cash'] == '1250.50'
        assert result['positions'][0]['quantity'] == '4'
        assert result['positions'][0]['price'] is None
        assert result['source'] == 'manual'
        # Removing all holdings while retaining explicit cash is valid.
        result['positions'] = []
        assert client.post('/api/portfolio', headers=headers, json={'snapshot': result}).status_code == 200
        assert client.get('/api/status').json()['snapshot']['positions'] == []


@pytest.mark.parametrize('change', [
    {'cash': '-1'}, {'cash': 'NaN'},
    {'positions': [{'symbol': 'ABC', 'quantity': '-1'}]},
    {'positions': [{'symbol': 'ABC', 'quantity': '1', 'price': 'Infinity'}]},
    {'positions': [{'symbol': 'ABC', 'quantity': '1'}, {'symbol': 'ABC', 'quantity': '2'}]},
])
def test_invalid_editor_input_does_not_save(tmp_path, change):
    with TestClient(create_app(tmp_path), base_url='http://127.0.0.1:8000') as client:
        page = client.get('/')
        token = re.search(r'name="csrf-token" content="([^"]+)"', page.text).group(1)
        response = client.post('/api/portfolio', headers={'x-csrf-token': token}, json={
            'snapshot': {'as_of': '2026-10-18T12:00:00Z', 'cash': '10', **change}})
        assert response.status_code in {400, 422}, response.text
        assert client.get('/api/status').json()['snapshot'] is None


def test_editor_javascript_preserves_draft_and_validates_amounts():
    """Execute the shipped editor functions with a small DOM adapter."""
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required to exercise browser JavaScript')
    script = Path(__file__).parents[1] / 'src/portfolio_analyzer/static/app.js'
    harness = r'''
const fs=require('fs'), vm=require('vm'), assert=require('assert/strict');
class Element {
  constructor(){this.children=[];this.dataset={};this.handlers={};this.value='';this.content='csrf';this._text='';}
  append(...items){for(const item of items){item.parent=this;this.children.push(item);}}
  replaceChildren(){this.children=[];}
  setAttribute(){}
  addEventListener(name,callback){this.handlers[name]=callback;}
  remove(){this.parent.children=this.parent.children.filter(child=>child!==this);}
  get firstElementChild(){return this.children[0];}
  querySelectorAll(){const visit=(item)=>[...(item.dataset.field?[item]:[]),...item.children.flatMap(visit)];return this.children.flatMap(visit);}
  get textContent(){return this._text;}
  set textContent(value){this._text=value;}
}
const elements=new Map();
global.document={querySelector(selector){if(!elements.has(selector)) elements.set(selector,new Element());return elements.get(selector);},querySelectorAll(){return [];},createElement(){return new Element();}};
vm.runInThisContext(fs.readFileSync(process.argv[1],'utf8').split('async function refresh(')[0]);
loadPortfolio({as_of:'2026-10-18T12:00:00Z',positions:[{symbol:'ABC',quantity:'2',price:null}],cash:'1234.50'},true);
assert.equal(portfolioDirty,true);
assert.equal(collectPortfolio().positions[0].price,null);
assert.equal(collectPortfolio().cash,'1234.50');
document.querySelector('#portfolio-cash').value='$2,500.75';
assert.equal(collectPortfolio().cash,'2500.75');
addHolding({symbol:'XYZ',quantity:'3',price:'$1,000.25'});
assert.equal(collectPortfolio().positions[1].price,'1000.25');
addHolding({symbol:'ABC',quantity:'1'});
assert.throws(()=>collectPortfolio(),/Duplicate/);
const row=document.querySelector('#holdings-rows').children[2];
row.children[3].children[0].handlers.click();
assert.equal(collectPortfolio().positions.length,2);
document.querySelector('#portfolio-cash').value='1,23';
assert.throws(()=>collectPortfolio(),/nonnegative/);
assert.throws(()=>amount('NaN','Price'),/nonnegative/);
assert.throws(()=>amount('-1','Price'),/nonnegative/);
assert.equal(amount('','Price',true),null);
loadPortfolio(null);
assert.equal(portfolioDirty,false);
assert.equal(collectPortfolio().positions.length,0);
loadPortfolio({as_of:'2026-10-18T12:00:00Z',positions:[{symbol:'ABC',quantity:'2',price:'42.50'}],cash:'10'});
assert.equal(collectPortfolio().positions[0].price,'42.50');
const firstRow=document.querySelector('#holdings-rows').children[0];
assert.equal(firstRow.querySelectorAll().some(field=>field.dataset.field==='price'),false);
assert.ok(firstRow.children[2].children[0].textContent.includes('42.50'));
'''
    result = subprocess.run([node, '-e', harness, str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_settings_validation_returns_named_fields(tmp_path):
    with TestClient(create_app(tmp_path), base_url='http://127.0.0.1:8000') as client:
        page = client.get('/')
        token = re.search(r'name="csrf-token" content="([^"]+)"', page.text).group(1)
        response = client.post('/api/settings', headers={'x-csrf-token': token}, json={'max_searches': 999})
        assert response.status_code == 400, response.text
        errors = response.json().get('errors') or []
        assert errors and any(entry['field'].endswith('max_searches') for entry in errors)
        assert all(entry['message'] for entry in errors)
        # The submitted value is never echoed back to the browser.
        assert '999' not in response.text


def test_dashboard_javascript_compacts_long_amounts_and_routes_views():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required to exercise browser JavaScript')
    script = Path(__file__).parents[1] / 'src/portfolio_analyzer/static/app.js'
    harness = r'''
const fs=require('fs'), vm=require('vm'), assert=require('assert/strict');
class Element {
  constructor(){this.children=[];this.dataset={};this.handlers={};this.value='';this.content='csrf';this._text='';}
  append(...items){for(const item of items){item.parent=this;this.children.push(item);}}
  replaceChildren(){this.children=[];}
  setAttribute(){}
  addEventListener(name,callback){this.handlers[name]=callback;}
  remove(){}
  get firstElementChild(){return this.children[0];}
  querySelectorAll(){return [];}
  get textContent(){return this._text;}
  set textContent(value){this._text=value;}
}
const elements=new Map();
global.document={querySelector(selector){if(!elements.has(selector)) elements.set(selector,new Element());return elements.get(selector);},querySelectorAll(){return [];},createElement(){return new Element();}};
vm.runInThisContext(fs.readFileSync(process.argv[1],'utf8').split('async function refresh(')[0]);
// Ordinary amounts stay exact; long amounts compact but keep the exact figure.
const short=displayFigure(3745.67);
assert.equal(short.text, short.exact);
assert.equal(short.text, exactMoney(3745.67));
const long=displayFigure(1234567.89);
assert.notEqual(long.text, long.exact);
assert.ok(long.exact.length>9);
assert.ok(long.text.length<long.exact.length);
assert.equal(long.exact, exactMoney(1234567.89));
assert.ok(compactFigure(1234567.89).length < exactMoney(1234567.89).length);
// Hash routes map to the addressable views.
assert.equal(viewFromHash('#portfolio'),'desk');
assert.equal(viewFromHash('#connections'),'connections');
assert.equal(viewFromHash('#preferences'),'preferences');
assert.equal(viewFromHash('#automation'),'automation');
assert.equal(viewFromHash('#portfolio-editor'),null);
assert.equal(VIEW_ROUTES.desk,'portfolio');
assert.equal(VIEW_ROUTES.automation,'automation');
// News legend reads clearly for every status the engine can emit.
for(const status of ['verified','discovery_only','no_verified_update','search_failed','not_checked']) assert.ok(NEWS_STATUS[status], status);
assert.equal(NEWS_STATUS.verified.dot,'verified');
assert.ok(NEWS_STATUS.discovery_only.label.includes('headline only (not verified)'));
'''
    result = subprocess.run([node, '-e', harness, str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
