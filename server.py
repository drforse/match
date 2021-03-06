from typing import Union

from elasticsearch import Elasticsearch
from flask import Flask, request
from image_match.elasticsearch_driver import SignatureES
from image_match.goldberg import ImageSignature
import json
import os
import sys
from functools import wraps
import certifi

# =============================================================================
# Globals

es_url = os.environ['ELASTICSEARCH_URL']
es_index = os.environ['ELASTICSEARCH_INDEX']
es_doc_type = os.environ['ELASTICSEARCH_DOC_TYPE']
es_login = os.environ.get('ELASTICSEARCH_LOGIN')
es_secret = os.environ.get('ELASTICSEARCH_SECRET')
all_orientations = os.environ['ALL_ORIENTATIONS']
default_min_score = float(os.environ['DEFAULT_MIN_SCORE'])

auth_token = os.environ.get('AUTH_TOKEN')

app = Flask(__name__)
es = Elasticsearch([es_url], verify_certs=True, timeout=60, max_retries=10, retry_on_timeout=True, ca_certs=certifi.where(), http_auth=(es_login, es_secret) if es_login else None)
ses = SignatureES(es, index=es_index, doc_type=es_doc_type)
gis = ImageSignature()

# Try to create the index and ignore IndexAlreadyExistsException
# if the index already exists
es.indices.create(index=es_index, ignore=400)


# =============================================================================
# Helpers
def ids_with_path(path):
    matches = es.search(index=es_index,
                        _source='_id',
                        q='path:' + json.dumps(path))
    return [m['_id'] for m in matches['hits']['hits']]


def paths_at_location(offset, limit):
    search = es.search(index=es_index,
                       from_=offset,
                       size=limit,
                       _source='path')
    return [h['_source']['path'] for h in search['hits']['hits']]


def count_images():
    return es.count(index=es_index)['count']


def delete_ids(ids):
    for i in ids:
        es.delete(index=es_index, doc_type=es_doc_type, id=i, ignore=404)


def dist_to_percent(dist):
    return (1 - dist) * 100


def dist_from_percent(percent: Union[float, int]):
    return 1 - percent / 100


def get_image(url_field, file_field):
    if url_field in request.form:
        return request.form[url_field], False
    else:
        return request.files[file_field].read(), True


def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if auth_token and request.args.get("token") != auth_token:
            return json.dumps({}), 403
        return f(*args, **kwargs)
    return decorated_function


# =============================================================================
# Routes

@app.route('/add', methods=['POST'])
@login_required
def add_handler():
    metadata = json.loads(request.form.get('metadata', 'null'))
    img, bs = get_image('url', 'image')
    path = request.form.get('filepath')
    if not path and bs:
        raise ValueError('filepath must be provided if image is passed as "image"')
    if not path:
        path = img

    old_ids = ids_with_path(path)
    ses.add_image(path, img, bytestream=bs, metadata=metadata)
    delete_ids(old_ids)

    return json.dumps({
        'status': 'ok',
        'error': [],
        'method': 'add',
        'result': []
    })


@app.route('/delete', methods=['DELETE'])
@login_required
def delete_handler():
    path = request.form['filepath']
    ids = ids_with_path(path)
    delete_ids(ids)
    return json.dumps({
        'status': 'ok',
        'error': [],
        'method': 'delete',
        'result': []
    })


@app.route('/search', methods=['POST'])
@login_required
def search_handler():
    img, bs = get_image('url', 'image')
    ao = request.form.get('all_orientations', all_orientations) == 'true'
    min_score = request.form.get('min_score', default_min_score)
    local_ses = SignatureES(es, index=es_index, doc_type=es_doc_type,
                            distance_cutoff=dist_from_percent(float(min_score)))

    matches = local_ses.search_image(
        path=img,
        all_orientations=ao,
        bytestream=bs
    )

    return json.dumps({
        'status': 'ok',
        'error': [],
        'method': 'search',
        'result': [{
            'score': dist_to_percent(m['dist']),
            'filepath': m['path'],
            'metadata': m['metadata']
        } for m in matches]
    })


@app.route('/compare', methods=['POST'])
@login_required
def compare_handler():
    img1, bs1 = get_image('url1', 'image1')
    img2, bs2 = get_image('url2', 'image2')
    img1_sig = gis.generate_signature(img1, bytestream=bs1)
    img2_sig = gis.generate_signature(img2, bytestream=bs2)
    score = dist_to_percent(gis.normalized_distance(img1_sig, img2_sig))

    return json.dumps({
        'status': 'ok',
        'error': [],
        'method': 'compare',
        'result': [{'score': score}]
    })


@app.route('/count', methods=['GET', 'POST'])
@login_required
def count_handler():
    count = count_images()
    return json.dumps({
        'status': 'ok',
        'error': [],
        'method': 'count',
        'result': [count]
    })


@app.route('/list', methods=['GET', 'POST'])
@login_required
def list_handler():
    if request.method == 'GET':
        offset = max(int(request.args.get('offset', 0)), 0)
        limit = max(int(request.args.get('limit', 20)), 0)
    else:
        offset = max(int(request.form.get('offset', 0)), 0)
        limit = max(int(request.form.get('limit', 20)), 0)
    paths = paths_at_location(offset, limit)

    return json.dumps({
        'status': 'ok',
        'error': [],
        'method': 'list',
        'result': paths
    })


@app.route('/ping', methods=['GET', 'POST'])
@login_required
def ping_handler():
    return json.dumps({
        'status': 'ok',
        'error': [],
        'method': 'ping',
        'result': []
    })


# =============================================================================
# Error Handling

@app.errorhandler(400)
def bad_request(e):
    return json.dumps({
        'status': 'fail',
        'error': ['bad request'],
        'method': '',
        'result': []
    }), 400


@app.errorhandler(404)
def page_not_found(e):
    return json.dumps({
        'status': 'fail',
        'error': ['not found'],
        'method': '',
        'result': []
    }), 404


@app.errorhandler(405)
def method_not_allowed(e):
    return json.dumps({
        'status': 'fail',
        'error': ['method not allowed'],
        'method': '',
        'result': []
    }), 405


@app.errorhandler(500)
def server_error(e):
    return json.dumps({
        'status': 'fail',
        'error': [str(e)],
        'method': '',
        'result': []
    }), 500
