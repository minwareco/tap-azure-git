import argparse
import os
import json
import collections
import time
from dateutil import parser
import pytz
import requests
import re
import singer
import singer.bookmarks as bookmarks
import singer.metrics as metrics

from singer import metadata

session = requests.Session()
logger = singer.get_logger()

REQUIRED_CONFIG_KEYS = ['start_date', 'user_name', 'access_token', 'org', 'repository']

KEY_PROPERTIES = {
    'commits': ['commitId'], # This is the SHA
    'pull_requests': ['artifactId'],
    'pull_request_threads': ['id'],
}

API_VESION = "6.0"

class AzureException(Exception):
    pass

class BadCredentialsException(AzureException):
    pass

class AuthException(AzureException):
    pass

class NotFoundException(AzureException):
    pass

class BadRequestException(AzureException):
    pass

class InternalServerError(AzureException):
    pass

class UnprocessableError(AzureException):
    pass

class NotModifiedError(AzureException):
    pass

class MovedPermanentlyError(AzureException):
    pass

class ConflictError(AzureException):
    pass

class RateLimitExceeded(AzureException):
    pass

ERROR_CODE_EXCEPTION_MAPPING = {
    301: {
        "raise_exception": MovedPermanentlyError,
        "message": "The resource you are looking for is moved to another URL."
    },
    304: {
        "raise_exception": NotModifiedError,
        "message": "The requested resource has not been modified since the last time you accessed it."
    },
    400:{
        "raise_exception": BadRequestException,
        "message": "The request is missing or has a bad parameter."
    },
    401: {
        "raise_exception": BadCredentialsException,
        "message": "Invalid authorization credentials."
    },
    403: {
        "raise_exception": AuthException,
        "message": "User doesn't have permission to access the resource."
    },
    404: {
        "raise_exception": NotFoundException,
        "message": "The resource you have specified cannot be found"
    },
    409: {
        "raise_exception": ConflictError,
        "message": "The request could not be completed due to a conflict with the current state of the server."
    },
    422: {
        "raise_exception": UnprocessableError,
        "message": "The request was not able to process right now."
    },
    429: {
        "raise_exception": RateLimitExceeded,
        "message": "Request rate limit exceeded."
    },
    500: {
        "raise_exception": InternalServerError,
        "message": "An error has occurred at Azure's end processing this request."
    },
    502: {
        "raise_exception": InternalServerError,
        "message": "Azure's service is not currently available."
    },
    503: {
        "raise_exception": InternalServerError,
        "message": "Azure's service is not currently available."
    },
    504: {
        "raise_exception": InternalServerError,
        "message": "Azure's service is not currently available."
    },
}

def get_bookmark(state, repo, stream_name, bookmark_key, start_date):
    repo_stream_dict = bookmarks.get_bookmark(state, repo, stream_name)
    if repo_stream_dict:
        return repo_stream_dict.get(bookmark_key)
    if start_date:
        return start_date
    return None

def raise_for_error(resp, source):
    error_code = resp.status_code
    try:
        response_json = resp.json()
    except Exception:
        response_json = {}

    if error_code == 404:
        details = ERROR_CODE_EXCEPTION_MAPPING.get(error_code).get("message")
        message = "HTTP-error-code: 404, Error: {}. Please refer \'{}\' for more details.".format(details, response_json.get("documentation_url"))
    else:
        message = "HTTP-error-code: {}, Error: {}".format(
            error_code, ERROR_CODE_EXCEPTION_MAPPING.get(error_code, {}).get("message", "Unknown Error") if response_json == {} else response_json)

    exc = ERROR_CODE_EXCEPTION_MAPPING.get(error_code, {}).get("raise_exception", AzureException)
    raise exc(message) from None

def calculate_seconds(epoch):
    current = time.time()
    return int(round((epoch - current), 0))

def rate_throttling(response):
    # TODO: check Retry-After
    """
    if int(response.headers['X-RateLimit-Remaining']) == 0:
        seconds_to_sleep = calculate_seconds(int(response.headers['X-RateLimit-Reset']))

        if seconds_to_sleep > 600:
            message = "API rate limit exceeded, please try after {} seconds.".format(seconds_to_sleep)
            raise RateLimitExceeded(message) from None

        logger.info("API rate limit exceeded. Tap will retry the data collection after %s seconds.", seconds_to_sleep)
        time.sleep(seconds_to_sleep)
    """
    pass

# pylint: disable=dangerous-default-value
def authed_get(source, url, headers={}):
    with metrics.http_request_timer(source) as timer:
        session.headers.update(headers)
        # Uncomment for debugging
        #logger.info("requesting {}".format(url))
        resp = session.request(method='get', url=url)
        if resp.status_code != 200:
            raise_for_error(resp, source)
        timer.tags[metrics.Tag.http_status_code] = resp.status_code
        rate_throttling(resp)
        return resp

PAGE_SIZE = 100
def authed_get_all_pages(source, url, page_param_name='', skip_param_name='', no_stop_indicator=False, headers={}):
    offset = 0
    if page_param_name:
        baseurl = url + '&{}={}'.format(page_param_name, PAGE_SIZE)
    else:
        baseurl = url
    continuationToken = ''
    while True:
        if skip_param_name == 'continuationToken':
            if continuationToken:
                cururl = baseurl + '&continuationToken={}'.format(continuationToken)
            else:
                cururl = baseurl
        elif page_param_name:
            cururl = baseurl + '&{}={}'.format(skip_param_name, offset)
        else:
            cururl = baseurl

        r = authed_get(source, cururl, headers)
        yield r

        # Look for a link header, and will have to update the URL parameters accordingly
        # link: <https://dev.azure.com/_apis/git/repositories/scheduled/commits>;rel="next"
        # Exception: for the changes endpoint, the server sends an empty link header, which just
        # seems buggy, but we have to handle it.
        if page_param_name and 'link' in r.headers and \
                ('rel="next"' in r.headers['link'] or '' == r.headers['link']):
            offset += PAGE_SIZE
        # You know what's awesome? How every endpoint implements pagination in a completely
        # different and inconsistent way.
        # Funny true story: After I wrote the comment above, I discovered that the pullrequests
        # endpoint actually has NO method of indicating the availability of more results, so you
        # literally have to just keep querying until you don't get any more data.
        elif no_stop_indicator:
            if r.json()['count'] < PAGE_SIZE:
                break
            else:
                offset += PAGE_SIZE
        elif 'x-ms-continuationtoken' in r.headers:
            continuationToken = r.headers['x-ms-continuationtoken']
        else:
            break

def get_abs_path(path):
    return os.path.join(os.path.dirname(os.path.realpath(__file__)), path)

def load_schemas():
    schemas = {}

    for filename in os.listdir(get_abs_path('schemas')):
        path = get_abs_path('schemas') + '/' + filename
        file_raw = filename.replace('.json', '')
        with open(path) as file:
            schema = json.load(file)
            refs = schema.pop("definitions", {})
            if refs:
                singer.resolve_schema_references(schema, refs)
            schemas[file_raw] = schema

    return schemas

class DependencyException(Exception):
    pass

def validate_dependencies(selected_stream_ids):
    errs = []
    msg_tmpl = ("Unable to extract '{0}' data, "
                "to receive '{0}' data, you also need to select '{1}'.")

    for main_stream, sub_streams in SUB_STREAMS.items():
        if main_stream not in selected_stream_ids:
            for sub_stream in sub_streams:
                if sub_stream in selected_stream_ids:
                    errs.append(msg_tmpl.format(sub_stream, main_stream))

    if errs:
        raise DependencyException(" ".join(errs))


def write_metadata(mdata, values, breadcrumb):
    mdata.append(
        {
            'metadata': values,
            'breadcrumb': breadcrumb
        }
    )

def populate_metadata(schema_name, schema):
    mdata = metadata.new()
    #mdata = metadata.write(mdata, (), 'forced-replication-method', KEY_PROPERTIES[schema_name])
    mdata = metadata.write(mdata, (), 'table-key-properties', KEY_PROPERTIES[schema_name])

    for field_name in schema['properties'].keys():
        if field_name in KEY_PROPERTIES[schema_name]:
            mdata = metadata.write(mdata, ('properties', field_name), 'inclusion', 'automatic')
        else:
            mdata = metadata.write(mdata, ('properties', field_name), 'inclusion', 'available')

    return mdata

def get_catalog():
    raw_schemas = load_schemas()
    streams = []

    for schema_name, schema in raw_schemas.items():

        # get metadata for each field
        mdata = populate_metadata(schema_name, schema)

        # create and add catalog entry
        catalog_entry = {
            'stream': schema_name,
            'tap_stream_id': schema_name,
            'schema': schema,
            'metadata' : metadata.to_list(mdata),
            'key_properties': KEY_PROPERTIES[schema_name],
        }
        streams.append(catalog_entry)

    return {'streams': streams}

def verify_repo_access(url_for_repo, repo):
    try:
        authed_get("verifying repository access", url_for_repo)
    except NotFoundException:
        # throwing user-friendly error message as it checks token access
        message = "HTTP-error-code: 404, Error: Please check the repository name \'{}\' or you do not have sufficient permissions to access this repository.".format(repo)
        raise NotFoundException(message) from None

def verify_access_for_repo(config):
    org = config['org']
    per_page = 1
    page = 1

    repositories = list(filter(None, config['repository'].split(' ')))

    for repo in repositories:
        logger.info("Verifying access of repository: %s", repo)
        reposplit = repo.split('/')
        project = reposplit[0]
        project_repo = reposplit[1]

        # https://dev.azure.com/${ORG}/${PROJECTNAME}/_apis/git/repositories/${REPONAME}/commits?searchCriteria.\$top=${PAGESIZE}\&searchCriteria.\$skip=${SKIP}\&api-version=${APIVERSION}
        url_for_repo = "https://dev.azure.com/{}/{}/_apis/git/repositories/{}/commits?" \
            "searchCriteria.$top={}&searchCriteria.$skip={}&api-version={}" \
            .format(org, project, project_repo, per_page, page - 1, API_VESION)

        # Verifying for Repo access
        verify_repo_access(url_for_repo, repo)

def do_discover(config):
    verify_access_for_repo(config)
    catalog = get_catalog()
    # dump catalog
    print(json.dumps(catalog, indent=2))

def write_commit_detail(org, project, project_repo, commit, schema, mdata, extraction_time):
    # Fetch the individual commit to obtain parents. This also provides pushes and other
    # properties, but we don't care about those for now.
    for commit_detail in authed_get_all_pages(
        'commits',
        "https://dev.azure.com/{}/{}/_apis/git/repositories/{}/commits/{}?" \
        "api-version={}" \
        .format(org, project, project_repo, commit['commitId'], API_VESION)
    ):
        detail_json = commit_detail.json()
        commit['parents'] = detail_json['parents']

    # TODO: include the first 100 changes in the request above and don't make the
    # additional changes request unless it's necessary. This will speed things up and
    # reduce the number of requests.

    # Augment each commit with file-level change data by querying the changes endpoint.
    commit['changes'] = []
    for commit_change_detail in authed_get_all_pages(
        'commits/changes',
        "https://dev.azure.com/{}/{}/_apis/git/repositories/{}/commits/{}/changes?" \
        "api-version={}" \
        .format(org, project, project_repo, commit['commitId'], API_VESION),
        'top',
        'skip'
    ):
        detail_json = commit_change_detail.json()
        commit['changes'].extend(detail_json['changes'])

    commit['_sdc_repository'] = "{}/{}".format(project, project_repo)
    with singer.Transformer() as transformer:
        rec = transformer.transform(commit, schema, metadata=metadata.to_map(mdata))
    singer.write_record('commits', rec, time_extracted=extraction_time)

def get_all_commits(schema, org, repo_path, state, mdata, start_date):
    '''
    https://docs.microsoft.com/en-us/rest/api/azure/devops/git/commits/get-commits?view=azure-devops-rest-6.0#gitcommitref

    Note: the change array looks like it is only included if the query has one result. So, it will
    nee to be fetched with commits/changes in a separate request in most cases.
    '''
    reposplit = repo_path.split('/')
    project = reposplit[0]
    project_repo = reposplit[1]

    bookmark = get_bookmark(state, repo_path, "commits", "since", start_date)
    if not bookmark:
        bookmark = '1970-01-01'

    with metrics.record_counter('commits') as counter:
        extraction_time = singer.utils.now()
        for response in authed_get_all_pages(
                'commits',
                "https://dev.azure.com/{}/{}/_apis/git/repositories/{}/commits?" \
                "api-version={}&searchCriteria.fromDate={}" \
                .format(org, project, project_repo, API_VESION, bookmark),
                'searchCriteria.$top',
                'searchCriteria.$skip'
        ):
            commits = response.json()
            for commit in commits['value']:

                write_commit_detail(org, project, project_repo, commit, schema, mdata, extraction_time)
                singer.write_bookmark(state, repo_path, 'commits', {'since': singer.utils.strftime(extraction_time)})
                counter.increment()

    return state

def get_all_pull_requests(schema, org, repo_path, state, mdata, start_date):
    '''
    https://docs.microsoft.com/en-us/rest/api/azure/devops/git/pull-requests/pull-requests-get-pull-requests?view=azure-devops-rest-6.1

    Note: commits will need to be fetched separately in a request to list PR commits
    '''
    reposplit = repo_path.split('/')
    project = reposplit[0]
    project_repo = reposplit[1]

    bookmark = get_bookmark(state, repo_path, "pull_requests", "since", start_date)
    if not bookmark:
        bookmark = '1970-01-01'
    bookmarkTime = parser.parse(bookmark)
    if bookmarkTime.tzinfo is None:
        bookmarkTime = pytz.UTC.localize(bookmarkTime)

    with metrics.record_counter('pull_requests') as counter:
        extraction_time = singer.utils.now()
        for response in authed_get_all_pages(
                'pull_requests',
                "https://dev.azure.com/{}/{}/_apis/git/repositories/{}/pullrequests?" \
                "api-version={}&searchCriteria.status=all" \
                .format(org, project, project_repo, API_VESION),
                '$top',
                '$skip',
                True # No link header to indicate availability of more data
        ):
            prs = response.json()['value']
            for pr in prs:
                # Since there is no fromDate parameter in the API, just filter out PRs that have been
                # closed prior to the the starting time
                if 'closedDate' in pr and parser.parse(pr['closedDate']) < bookmarkTime:
                    continue

                # List the PR commits to include those
                pr['commits'] = []
                for pr_commit_response in authed_get_all_pages(
                        'pull_requests/commits',
                        "https://dev.azure.com/{}/{}/_apis/git/repositories/{}/pullrequests/{}/commits?" \
                        "api-version={}" \
                        .format(org, project, project_repo, pr['pullRequestId'], API_VESION),
                        '$top',
                        'continuationToken'
                ):
                    pr_commits = pr_commit_response.json()
                    pr['commits'].extend(pr_commits['value'])

                    # Note: These commits will already have their detail fetched by the commits
                    # endpoint (even if they are in an unmerged PR or abandoned), so we don't need
                    # to fetch more info here -- we only need to provide the shallow references.

                # Write out the pull request info

                pr['_sdc_repository'] = repo_path

                # So pullRequestId isn't actually unique. There is a 'artifactId' parameter that is
                # unique, but, surprise surprise, the API doesn't actually include this property
                # when listing multiple PRs, so we need to construct it from the URL. Hilariously,
                # this ID also contains %2f for the later slashes instead of actual slashes.
                # Get the project_id and repo_id from the URL
                # TODO: not sure what type of exception to throw here if if the url isn't present
                # and matching this format.
                url_search = re.search('dev\\.azure\\.com/\w+/([-\w]+)/_apis/git/repositories/([-\w]+)', pr['url'])
                project_id = url_search.group(1)
                repo_id = url_search.group(2)
                pr['artifactId'] = "vstfs:///Git/PullRequestId/{}%2f{}%2f{}" \
                    .format(project_id, repo_id, pr['pullRequestId'])

                with singer.Transformer() as transformer:
                    rec = transformer.transform(pr, schema, metadata=metadata.to_map(mdata))
                singer.write_record('pull_requests', rec, time_extracted=extraction_time)
                singer.write_bookmark(state, repo_path, 'pull_requests', {'since': singer.utils.strftime(extraction_time)})
                counter.increment()

    return state


def get_selected_streams(catalog):
    '''
    Gets selected streams.  Checks schema's 'selected'
    first -- and then checks metadata, looking for an empty
    breadcrumb and mdata with a 'selected' entry
    '''
    selected_streams = []
    for stream in catalog['streams']:
        stream_metadata = stream['metadata']
        if stream['schema'].get('selected', False):
            selected_streams.append(stream['tap_stream_id'])
        else:
            for entry in stream_metadata:
                # stream metadata will have empty breadcrumb
                if not entry['breadcrumb'] and entry['metadata'].get('selected',None):
                    selected_streams.append(stream['tap_stream_id'])

    return selected_streams

def get_stream_from_catalog(stream_id, catalog):
    for stream in catalog['streams']:
        if stream['tap_stream_id'] == stream_id:
            return stream
    return None

SYNC_FUNCTIONS = {
    'commits': get_all_commits,
    'pull_requests': get_all_pull_requests,
}

SUB_STREAMS = {
    'pull_requests': ['pull_request_threads'],
}

def do_sync(config, state, catalog):
    '''
    The state structure will be the following:
    {
      "bookmarks": {
        "project1/repo1": {
          "commits": {
            "since": "2018-11-14T13:21:20.700360Z"
          }
        },
        "project2/repo2": {
          "commits": {
            "since": "2018-11-14T13:21:20.700360Z"
          }
        }
      }
    }
    '''
    start_date = config['start_date'] if 'start_date' in config else None
    # get selected streams, make sure stream dependencies are met
    selected_stream_ids = get_selected_streams(catalog)
    validate_dependencies(selected_stream_ids)

    org = config['org']

    repositories = list(filter(None, config['repository'].split(' ')))

    singer.write_state(state)

    #pylint: disable=too-many-nested-blocks
    for repo in repositories:
        logger.info("Starting sync of repository: %s", repo)
        for stream in catalog['streams']:
            stream_id = stream['tap_stream_id']
            stream_schema = stream['schema']
            mdata = stream['metadata']

            # if it is a "sub_stream", it will be sync'd by its parent
            if not SYNC_FUNCTIONS.get(stream_id):
                continue

            # if stream is selected, write schema and sync
            if stream_id in selected_stream_ids:
                singer.write_schema(stream_id, stream_schema, stream['key_properties'])

                # get sync function and any sub streams
                sync_func = SYNC_FUNCTIONS[stream_id]
                sub_stream_ids = SUB_STREAMS.get(stream_id, None)

                # sync stream
                if not sub_stream_ids:
                    state = sync_func(stream_schema, org, repo, state, mdata, start_date)

                # handle streams with sub streams
                else:
                    stream_schemas = {stream_id: stream_schema}

                    # get and write selected sub stream schemas
                    for sub_stream_id in sub_stream_ids:
                        if sub_stream_id in selected_stream_ids:
                            sub_stream = get_stream_from_catalog(sub_stream_id, catalog)
                            stream_schemas[sub_stream_id] = sub_stream['schema']
                            singer.write_schema(sub_stream_id, sub_stream['schema'],
                                                sub_stream['key_properties'])

                    # sync stream and its sub streams
                    state = sync_func(stream_schemas, org, repo, state, mdata, start_date)

                singer.write_state(state)

@singer.utils.handle_top_exception(logger)
def main():
    args = singer.utils.parse_args(REQUIRED_CONFIG_KEYS)

    # Initialize basic auth
    user_name = args.config['user_name']
    access_token = args.config['access_token']
    session.auth = (user_name, access_token)

    if args.discover:
        do_discover(args.config)
    else:
        catalog = args.properties if args.properties else get_catalog()
        do_sync(args.config, args.state, catalog)

if __name__ == '__main__':
    main()
