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


NODE_FALLBACK = Path('/mnt/c/Program Files/nodejs/node.exe')


def find_node():
    """Node on PATH, or the known Windows install reached from WSL."""
    node = shutil.which('node')
    if node:
        return node, lambda path: path
    if NODE_FALLBACK.exists():
        return str(NODE_FALLBACK), lambda path: str(path).replace('/mnt/c/', 'C:\\').replace('/', '\\')
    return None, lambda path: path


def run_js(harness):
    node, to_node_path = find_node()
    if not node:
        pytest.skip('Node is required to exercise browser JavaScript')
    script = Path(__file__).parents[1] / 'src/portfolio_analyzer/static/app.js'
    result = subprocess.run([node, '-e', harness, to_node_path(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


DOM_HARNESS = r'''
const fs=require('fs'), vm=require('vm'), assert=require('assert/strict');
class Element {
  constructor(){this.classList={add(){}};this.children=[];this.dataset={};this.handlers={};this.value='';this.content='csrf';this._text='';this.className='';this.classList={add(){},remove(){},toggle(){}};}
  append(...items){for(const item of items){item.parent=this;this.children.push(item);}}
  replaceChildren(){this.children=[];}
  setAttribute(){}
  addEventListener(name,callback){this.handlers[name]=callback;}
  remove(){if(this.parent)this.parent.children=this.parent.children.filter(child=>child!==this);}
  get firstElementChild(){return this.children[0];}
  get childElementCount(){return this.children.length;}
  querySelectorAll(){return [];}
  closest(){return null;}
  focus(){}
  scrollIntoView(){}
  get textContent(){return textOf(this);}
  set textContent(value){this._text=String(value);this.children=[];}
}
function textOf(el){return [el._text||'',...el.children.map(textOf)].filter(Boolean).join(' ');}
function nodesOf(root,acc=[]){acc.push({className:root.className||'',text:(root._text||'')});for(const child of root.children)nodesOf(child,acc);return acc;}
const elements=new Map();
global.document={querySelector(selector){if(!elements.has(selector)) elements.set(selector,new Element());return elements.get(selector);},querySelectorAll(){return [];},createElement(){return new Element();}};
vm.runInThisContext(fs.readFileSync(process.argv[1],'utf8').split('async function refresh(')[0]);
'''


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
    harness = r'''
const fs=require('fs'), vm=require('vm'), assert=require('assert/strict');
class Element {
  constructor(){this.classList={add(){}};this.children=[];this.dataset={};this.handlers={};this.value='';this.content='csrf';this._text='';}
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
    run_js(harness)


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
