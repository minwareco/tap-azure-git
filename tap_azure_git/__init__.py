# Standard Library
import argparse
import asyncio
import collections
import functools
import gc
import json
import os
import re
import time
import urllib.parse
from datetime import datetime
from operator import itemgetter
from contextlib import suppress

# 3rd Party
import psutil
import pytz
import requests
import backoff
import singer
import singer.bookmarks as bookmarks
import singer.metrics as metrics
from dateutil import parser
from minware_singer_utils import GitLocal, SecureLogger
from singer import metadata

session = requests.Session()
logger = SecureLogger(singer.get_logger())

REQUIRED_CONFIG_KEYS = ['start_date', 'user_name', 'access_token', 'org', 'repository']

KEY_PROPERTIES = {
    'commits': ['id'],
    'pull_requests': ['artifactId'],
    'pull_request_threads': ['id'],
    'refs': ['id'],
    'commit_files': ['id'],
    'commit_files_meta': ['id'],
    'repositories': ['id'],
    'annotated_tags': ['id'],
    'builds': ['_sdc_id'],
    'build_timelines': ['_sdc_id'],
    'pipelines': ['_sdc_id'],
    'runs': ['_sdc_id']
}

API_VERSION = "6.0"
API_VERSION_7_1 = "7.1"

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
        "message": "Invalid authorization credentials. Please check that your access token is " \
            "correct, has not expired, and has read access to the 'Code' and 'Pull Request Threads' scopes."
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

process_globals = True

def get_bookmark(state, repo, stream_name, bookmark_key, default_value=None):
    repo_stream_dict = bookmarks.get_bookmark(state, repo, stream_name)
    if repo_stream_dict:
        return repo_stream_dict.get(bookmark_key)
    if default_value:
        return default_value
    return None

def raise_for_error(resp, source, url):
    error_code = resp.status_code
    try:
        response_json = resp.json()
    except Exception:
        response_json = {}

    # TODO: if/when we hook this up to exception tracking, report the URL as metadat rather than as
    # part of the exception message.

    if error_code == 404:
        details = ERROR_CODE_EXCEPTION_MAPPING.get(error_code).get("message")
        message = "HTTP-error-code: 404, Error: {}. Please check that the following URL is valid "\
            "and you have permission to access it: {}".format(details, url)
    else:
        message = "HTTP-error-code: {}, Error: {} Url: {}".format(
            error_code, ERROR_CODE_EXCEPTION_MAPPING.get(error_code, {}) \
            .get("message", "Unknown Error") if response_json == {} else response_json, \
            url)

        # Map HTTP version numbers
        http_versions = {
            10: "HTTP/1.0",
            11: "HTTP/1.1",
            20: "HTTP/2.0"
        }
        http_version = http_versions.get(resp.raw.version, "Unknown")
        message += "\nResponse Details: \n\t{} {}".format(http_version, resp.status_code)
        for key, value in resp.headers.items():
            message += "\n\t{}: {}".format(key, value)
        if resp.content:
            message += "\n\t{}".format(resp.text)

    exc = ERROR_CODE_EXCEPTION_MAPPING.get(error_code, {}).get("raise_exception", AzureException)
    raise exc(message) from None

def request(source, url, method='GET'):
    """
    This function performs an HTTP request and implements a robust retry mechanism
    to handle transient errors and optimize the request's success rate.

    Key Features:
    1. **Retry on Predicate (Retry-After Header)**:
    - Retries when the response contains a "Retry-After" header, which is commonly used
        to indicate when a client should retry a request.
    - This is particularly useful for Azure, which may include "Retry-After" headers
        for both 429 (Too Many Requests) and 5xx (Server Error) responses.
    - A maximum of 8 retry attempts is made, respecting the "Retry-After" value
        specified in the header.

    2. **Retry on Exceptions**:
    - Retries when a `requests.exceptions.RequestException` occurs.
    - Uses an exponential backoff strategy (doubling the delay between retries)
        with a maximum of 5 retry attempts.
    - Stops retrying if the exception is due to a client-side error (HTTP 4xx),
        as these are typically non-recoverable. The single exception is 429 (Too Many Requests).

    Parameters:
    - `source` (str): The source that is triggering the HTTP request.
    - `url` (str): The URL to which the HTTP request is sent.
    - `method` (str, optional): The HTTP method to use (default is 'GET').

    Returns:
    - `response` (requests.Response): The HTTP response object.

    Notes:
    - This function leverages the `backoff` library for retry strategies and logging.
    - A session object (assumed to be pre-configured) is used for making the HTTP request.
    """

    exponential_factor = 5
    tries = 0
    def backoff_value(response):
        nonlocal tries
        with suppress(TypeError, ValueError, AttributeError):
            return int(response.headers.get("Retry-After"))
        backoff_time = exponential_factor * (2 ** tries)
        tries += 1
        return backoff_time

    def execute_request(source, url, method='GET'):
        with metrics.http_request_timer(source) as timer:
            timer.tags['url'] = url

            response = session.request(method=method, url=url)

            timer.tags[metrics.Tag.http_status_code] = response.status_code
            timer.tags['header_retry-after'] = response.headers.get('retry-after')
            timer.tags['header_x-ratelimit-resource'] = response.headers.get('x-ratelimit-resource')
            timer.tags['header_x-ratelimit-delay'] = response.headers.get('x-ratelimit-delay')
            timer.tags['header_x-ratelimit-limit'] = response.headers.get('x-ratelimit-limit')
            timer.tags['header_x-ratelimit-remaining'] = response.headers.get('x-ratelimit-remaining')
            timer.tags['header_x-ratelimit-reset'] = response.headers.get('x-ratelimit-reset')

            return response

    backoff_on_exception = backoff.on_exception(backoff.expo,
                      (requests.exceptions.RequestException),
                      max_tries=7,
                      max_time=3600,
                      giveup=lambda e: e.response is not None and e.response.status_code != 429 and 400 <= e.response.status_code < 500,
                      factor=exponential_factor,
                      jitter=backoff.random_jitter,
                      logger=logger)

    backoff_on_predicate = backoff.on_predicate(backoff.runtime,
                      predicate=lambda r: r.headers.get("Retry-After", None) != None or r.status_code >= 300,
                      max_tries=7,
                      max_time=3600,
                      value=backoff_value,
                      jitter=backoff.random_jitter,
                      logger=logger)

    return backoff_on_exception(backoff_on_predicate(execute_request))(source, url, method)

# pylint: disable=dangerous-default-value
def authed_get(source, url, headers={}):
    session.headers.update(headers)
    resp = request(source, url, method='get')

    if resp.status_code not in [200, 204]:
        raise_for_error(resp, source, url)

    return resp

PAGE_SIZE = 100
def authed_get_all_pages(source, url, page_param_name='', skip_param_name='',
        no_stop_indicator=False, iterate_state=None, headers={}):
    offset = 0
    if iterate_state and 'offset' in iterate_state:
        offset = iterate_state['offset']
    if page_param_name:
        baseurl = url + '&{}={}'.format(page_param_name, PAGE_SIZE)
    else:
        baseurl = url
    continuationToken = ''
    if iterate_state and 'continuationToken' in iterate_state:
        continuationToken = iterate_state['continuationToken']
    isDone = False
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
                isDone = True
                break
            else:
                offset += PAGE_SIZE
        elif 'x-ms-continuationtoken' in r.headers:
            continuationToken = r.headers['x-ms-continuationtoken']
        else:
            isDone = True
            break
        # If there's an iteration state, we only want to do one iteration
        if iterate_state:
            break

    # Populate the iterate state if it's present
    if iterate_state:
        iterate_state['stop'] = isDone
        iterate_state['offset'] = offset
        iterate_state['continuationToken'] = continuationToken

def get_abs_path(path):
    return os.path.join(os.path.dirname(os.path.realpath(__file__)), path)

def remove_definitions_prefix(obj):
    # If the object is a dictionary, check each key
    if isinstance(obj, dict):
        new_obj = {}
        for key, value in obj.items():
            if key == "$ref" and isinstance(value, str) and value.startswith("#/definitions/"):
                # Remove the prefix
                new_obj[key] = value.replace("#/definitions/", "")
            else:
                # Recursively apply the function to nested dictionaries/lists
                new_obj[key] = remove_definitions_prefix(value)
        return new_obj
    # If the object is a list, apply the function to each element
    elif isinstance(obj, list):
        return [remove_definitions_prefix(item) for item in obj]
    # Return the object itself if it's neither a dictionary nor a list
    else:
        return obj

def load_schemas():
    schemas = {}

    for filename in os.listdir(get_abs_path('schemas')):
        path = get_abs_path('schemas') + '/' + filename
        file_raw = filename.replace('.json', '')
        with open(path) as file:
            schema = json.load(file)
            refs = schema.pop("definitions", {})
            if refs:
                schema = singer.resolve_schema_references(
                    remove_definitions_prefix(schema),
                    remove_definitions_prefix(refs)
                )
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


@functools.cache
def get_repository(org, project, repo):
    # https://learn.microsoft.com/en-us/rest/api/azure/devops/git/repositories/get-repository?view=azure-devops-rest-7.1&tabs=HTTP
    return authed_get(
        "repository",
        f"https://dev.azure.com/{org}/{project}/_apis/git/repositories/{repo}"
    ).json()

def verify_repo_access(url_for_repo, repo, config):
    try:
        authed_get("verifying repository access", url_for_repo)
    except NotFoundException:
        # throwing user-friendly error message as it checks token access
        org = config['org']
        user_name = config['user_name']
        reposplit = repo.split('/')
        projectname = reposplit[0]
        reponame = reposplit[1]
        message = "HTTP-error-code: 404, Error: Please check the repository \'{}\' exists in " \
            "project \'{}\' for org \'{}\', and that user \'{}\' has permission to access it." \
            .format(reponame, projectname, org, user_name)
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
            .format(org, project, project_repo, per_page, page - 1, API_VERSION)

        # Verifying for Repo access
        verify_repo_access(url_for_repo, repo, config)

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
        .format(org, project, project_repo, commit['commitId'], API_VERSION)
    ):
        detail_json = commit_detail.json()
        commit['parents'] = detail_json['parents']

    # We no longer want to fetch changes here and instead will do it with GitLocal
    commit['_sdc_repository'] = "{}/{}/{}".format(org, project, project_repo)
    commit['id'] = "{}/{}/{}/{}".format(org, project, project_repo, commit['commitId'])
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

    # This will only be use if it's our first run and we don't have any fetchedCommits. See below.
    bookmark = get_bookmark(state, repo_path, "commits", "since", start_date)
    if not bookmark:
        bookmark = '1970-01-01'

    # Get the set of all commits we have fetched previously
    fetchedCommits = get_bookmark(state, repo_path, "commits", "fetchedCommits")
    if not fetchedCommits:
        fetchedCommits = {}
    else:
        # We have run previously, so we don't want to use the time-based bookmark becuase it could
        # skip commits that are pushed after they are committed. So, reset the 'since' bookmark back
        # to the beginning of time and rely solely on the fetchedCommits bookmark.
        bookmark = '1970-01-01'

    # We don't want newly fetched commits to update the state if we fail partway through, because
    # this could lead to commits getting marked as fetched when their parents are never fetched. So,
    # copy the dict.
    fetchedCommits = fetchedCommits.copy()
    # Maintain a list of parents we are waiting to see
    missingParents = {}

    with metrics.record_counter('commits') as counter:
        extraction_time = singer.utils.now()
        iterate_state = {'not': 'empty'}
        count = 1
        while True:
            count += 1
            response = authed_get_all_pages(
                'commits',
                "https://dev.azure.com/{}/{}/_apis/git/repositories/{}/commits?" \
                "api-version={}&searchCriteria.fromDate={}" \
                .format(org, project, project_repo, API_VERSION, bookmark),
                'searchCriteria.$top',
                'searchCriteria.$skip',
                iterate_state=iterate_state
            )

            commits = list(response)[0].json()
            for commit in commits['value']:
                # Skip commits we've already imported
                if commit['commitId'] in fetchedCommits:
                    continue
                # Will also populate the 'parents' sha list
                write_commit_detail(org, project, project_repo, commit, schema, mdata, extraction_time)

                # Record that we have now fetched this commit
                fetchedCommits[commit['commitId']] = 1
                # No longer a missing parent
                missingParents.pop(commit['commitId'], None)

                # Keep track of new missing parents
                for parent in commit['parents']:
                    if not parent in fetchedCommits:
                        missingParents[parent] = 1

                counter.increment()

            # If there are no missing parents, then we are done prior to reaching the lst page
            if not missingParents:
                break
            # Else if we have reached the end of our data but not found the parents, then we have a
            # problem
            elif iterate_state['stop']:
                raise AzureException('Some commit parents never found: ' + \
                    ','.join(missingParents.keys()))
            # Otherwise, proceed to fetch the next page with the next iteration state

    # Don't write until the end so that we don't record fetchedCommits if we fail and never get
    # their parents.
    singer.write_bookmark(state, repo_path, 'commits', {
        'since': singer.utils.strftime(extraction_time),
        'fetchedCommits': fetchedCommits
    })

    return state


def get_commit_detail_local(commit, gitLocalRepoPath, gitLocal):
    try:
        changes = gitLocal.getCommitDiff(gitLocalRepoPath, commit['sha'])
        commit['files'] = changes
    except Exception as e:
        # This generally shouldn't happen since we've already fetched and checked out the head
        # commit successfully, so it probably indicates some sort of system error. Just let it
        # bubbl eup for now.
        raise e

def get_commit_changes(commit, sdcRepository, gitLocalRepoPath, gitLocal):
    get_commit_detail_local(commit, gitLocalRepoPath, gitLocal)
    commit['_sdc_repository'] = sdcRepository
    commit['id'] = '{}/{}'.format(sdcRepository, commit['sha'])
    return commit

async def getChangedfilesForCommits(commits, sdcRepository, gitLocalRepoPath, gitLocal):
    coros = []
    for commit in commits:
        changesCoro = asyncio.to_thread(get_commit_changes, commit, sdcRepository, gitLocalRepoPath, gitLocal)
        coros.append(changesCoro)
    results = await asyncio.gather(*coros)
    return results

def get_all_heads_for_commits(repo_path):
    # TODO: implement this for like we did for gitlab
    '''
    Gets a list of all SHAs to use as heads for importing lists of commits. Includes all branches
    and PRs (both base and head) as well as the main branch to get all potential starting points.

    default_branch_name = get_repo_metadata(repo_path)['default_branch']

    # If this data has already been populated with get_all_branches, don't duplicate the work.
    if not repo_path in BRANCH_CACHE:
        cur_cache = {}
        BRANCH_CACHE[repo_path] = cur_cache
        for response in authed_get_all_pages(
            'branches',
            'https://api.github.com/repos/{}/branches?per_page=100'.format(repo_path)
        ):
            branches = response.json()
            for branch in branches:
                isdefault = branch['name'] == default_branch_name
                cur_cache[branch['name']] = {
                    'sha': branch['commit']['sha'],
                    'isdefault': isdefault,
                    'name': branch['name']
                }

    if not repo_path in PR_CACHE:
        cur_cache = {}
        PR_CACHE[repo_path] = cur_cache
        for response in authed_get_all_pages(
            'pull_requests',
            'https://api.github.com/repos/{}/pulls?per_page=100&state=all'.format(repo_path)
        ):
            pull_requests = response.json()
            for pr in pull_requests:
                pr_num = pr.get('number')
                cur_cache[str(pr_num)] = {
                    'pr_num': str(pr_num),
                    'base_sha': pr['base']['sha'],
                    'base_ref': pr['base']['ref'],
                    'head_sha': pr['head']['sha'],
                    'head_ref': pr['head']['ref']
                }

    # Now build a set of all potential heads
    head_set = {}
    for key, val in BRANCH_CACHE[repo_path].items():
        head_set[val['sha']] = 'refs/heads/' + val['name']
    for key, val in PR_CACHE[repo_path].items():
        head_set[val['head_sha']] = 'refs/pull/' + val['pr_num'] + '/head'
        # There could be a PR into a branch that has since been deleted and this is our only record
        # of its head, so include it
        head_set[val['base_sha']] = 'refs/heads/' + val['base_ref']
    return head_set
    '''

def get_all_commit_files(schemas, org, repo_path, state, mdata, start_date, gitLocal, heads, commits_only=False):
    '''
    repo_path should be the full _sdc_repository path of {org}/{project}/{repo}
    '''
    reposplit = repo_path.split('/')
    project = reposplit[0]
    project_repo = reposplit[1]

    sdcRepository = '{}/{}/{}'.format(org, project, project_repo)
    gitLocalRepoPath = urllib.parse.quote('{}/{}/_git/{}'.format(org, project, project_repo))

    stream_name = 'commit_files_meta' if commits_only else 'commit_files'
    bookmark = get_bookmark(state, repo_path, stream_name, "since", start_date)
    if not bookmark:
        bookmark = '1970-01-01'

    # Get the set of all commits we have fetched previously
    fetchedCommits = get_bookmark(state, repo_path, stream_name, "fetchedCommits")
    if not fetchedCommits:
        fetchedCommits = {}
    else:
        # We have run previously, so we don't want to use the time-based bookmark becuase it could
        # skip commits that are pushed after they are committed. So, reset the 'since' bookmark back
        # to the beginning of time and rely solely on the fetchedCommits bookmark.
        bookmark = '1970-01-01'

    logger.info('Found {} fetched commits in state.'.format(len(fetchedCommits)))

    # We don't want newly fetched commits to update the state if we fail partway through, because
    # this could lead to commits getting marked as fetched when their parents are never fetched. So,
    # copy the dict.
    fetchedCommits = fetchedCommits.copy()

    # Set this here for updating the state when we don't run any queries
    extraction_time = singer.utils.now()

    # Get all of the branch heads to use for querying commits
    #heads = get_all_heads_for_commits(repo_path)
    localHeads = gitLocal.getReferences(gitLocalRepoPath)
    headsToCommits = dict()

    with metrics.record_counter('annotated_tags') as counter:
        for h in localHeads:
            ref = localHeads[h]
            if ref['type'] == 'tag':
                annotated_tag = gitLocal.getAnnotatedTag(gitLocalRepoPath, ref['sha'])
                headsToCommits[h] = {
                    'type': 'tag',
                    'sha': annotated_tag['tagId'],
                    'commit_sha': annotated_tag['commitId']
                }

                if 'annotated_tags' in schemas:
                    annotated_tag_record = {
                        **annotated_tag,
                        '_sdc_repository': sdcRepository,
                        'id': '{}/{}'.format(sdcRepository, annotated_tag['tagId'])
                    }
                    annotated_tag_record['tagger']['date'] = \
                        annotated_tag_record['tagger']['date'].isoformat()
                    with singer.Transformer() as transformer:
                        rec = transformer.transform(annotated_tag_record, schemas['annotated_tags'],
                            metadata=metadata.to_map(mdata))
                    counter.increment()
                    singer.write_record('annotated_tags', rec, time_extracted=extraction_time)

            else:
                headsToCommits[h] = {
                    'type': 'commit',
                    'sha': ref['sha'],
                    'commit_sha': ref['sha']
                }

    for k in heads:
        headsToCommits[k] = {
            'type': 'commit',
            'sha': heads[k],
            'commit_sha': heads[k]
        }

    count = 0
    # The large majority of PRs are less than this many commits
    LOG_PAGE_SIZE = 10000
    with metrics.record_counter(stream_name) as counter:
        # First, walk through all the heads and queue up all the commits that need to be imported
        commitQ = []

        for headRef in headsToCommits:
            count += 1
            if count % 10 == 0:
                process = psutil.Process(os.getpid())
                logger.info('Processed heads {}/{}, {} bytes'.format(count, len(heads),
                    process.memory_info().rss))

            objectType, sha, headSha = itemgetter('type', 'sha', 'commit_sha')(headsToCommits[headRef])

            # Emit the ref record as well if it's not for a pull request
            if not ('refs/pull' in headRef):
                refRecord = {
                    'id': '{}/{}'.format(sdcRepository, headRef),
                    '_sdc_repository': sdcRepository,
                    'ref': headRef,
                    'sha': sha,
                    'type': objectType
                }
                with singer.Transformer() as transformer:
                    rec = transformer.transform(refRecord, schemas['refs'],
                        metadata=metadata.to_map(mdata))
                singer.write_record('refs', rec, time_extracted=extraction_time)

            # If the head commit has already been synced, then skip.
            if headSha in fetchedCommits:
                #logger.info('Head already fetched {} {}'.format(headRef, headSha))
                continue

            # Maintain a list of parents we are waiting to see
            missingParents = {}

            # Verify that this commit exists in our mirrored repo
            commitHasLocal = gitLocal.hasLocalCommit(gitLocalRepoPath, headSha)
            if not commitHasLocal:
                logger.warning('MISSING REF/COMMIT {}/{}/{}'.format(gitLocalRepoPath, headRef,
                    headSha))
                # Skip this now that we're mirroring everything. We shouldn't have anything that's
                # missing from github's API
                continue


            offset = 0
            newlyFetchedCommits = {}
            while True:
                commits = gitLocal.getCommitsFromHeadPyGit(gitLocalRepoPath, headSha,
                    limit = LOG_PAGE_SIZE, offset = offset, skipAtCommits=fetchedCommits)

                extraction_time = singer.utils.now()
                for commit in commits:
                    # Skip commits we've already imported
                    if commit['sha'] in fetchedCommits or commit['sha'] in newlyFetchedCommits:
                        continue

                    commitQ.append(commit)

                    # Record that we have now fetched this commit
                    newlyFetchedCommits[commit['sha']] = 1
                    # No longer a missing parent
                    missingParents.pop(commit['sha'], None)

                    # Keep track of new missing parents
                    for parent in commit['parents']:
                        if not parent['sha'] in fetchedCommits and not parent['sha'] in newlyFetchedCommits:
                            missingParents[parent['sha']] = 1

                # If there are no missing parents, then we are done prior to reaching the lst page
                if not missingParents:
                    break
                elif len(commits) > 0:
                    offset += LOG_PAGE_SIZE
                # Else if we have reached the end of our data but not found the parents, then we have a
                # problem
                else:
                    raise AzureException('Some commit parents never found: ' + \
                        ','.join(missingParents.keys()))
                # Otherwise, proceed to fetch the next page with the next iteration state

            # After successfully processing all commits for this head, add them to fetchedCommits
            fetchedCommits.update(newlyFetchedCommits)

        # Now run through all the commits in parallel
        gc.collect()
        process = psutil.Process(os.getpid())
        logger.info('Processing {} commits, mem(mb) {}'.format(len(commitQ),
            process.memory_info().rss / (1024 * 1024)))

        # Run in batches
        i = 0
        BATCH_SIZE = 2
        PRINT_INTERVAL = 8
        totalCommits = len(commitQ)
        finishedCount = 0

        while len(commitQ) > 0:
            # Slice off the queue to avoid memory leaks
            curQ = commitQ[0:BATCH_SIZE]
            commitQ = commitQ[BATCH_SIZE:]
            if commits_only:
                # In commit-only mode, skip file diff processing and set essential metadata
                for commit in curQ:
                    commit['files'] = []
                    commit['_sdc_repository'] = sdcRepository
                    commit['id'] = '{}/{}'.format(sdcRepository, commit['sha'])
                changedFileList = curQ
            else:
                changedFileList = asyncio.run(getChangedfilesForCommits(curQ, sdcRepository, gitLocalRepoPath, gitLocal))
            for commitfiles in changedFileList:
                with singer.Transformer() as transformer:
                    rec = transformer.transform(commitfiles, schemas[stream_name],
                        metadata=metadata.to_map(mdata))
                counter.increment()
                singer.write_record(stream_name, rec, time_extracted=extraction_time)

            finishedCount += BATCH_SIZE
            if i % (BATCH_SIZE * PRINT_INTERVAL) == 0:
                curQ = None
                changedFileList = None
                gc.collect()
                process = psutil.Process(os.getpid())
                logger.info('Imported {}/{} commits, {}/{} MB'.format(finishedCount, totalCommits,
                    process.memory_info().rss / (1024 * 1024),
                    process.memory_info().data / (1024 * 1024)))


    # Don't write until the end so that we don't record fetchedCommits if we fail and never get
    # their parents.
    singer.write_bookmark(state, repo_path, stream_name, {
        'since': singer.utils.strftime(extraction_time),
        'fetchedCommits': fetchedCommits
    })

    return state


def get_threads_for_pr(prid, schema, org, repo_path, state, mdata):
    '''
    https://docs.microsoft.com/en-us/rest/api/azure/devops/git/pull-request-threads/pull-request-threads-list?view=azure-devops-rest-6.0

    WARNING: This API has no paging support whatsoever, so hope that there aren't any limits.
    '''
    reposplit = repo_path.split('/')
    project = reposplit[0]
    project_repo = reposplit[1]

    for response in authed_get_all_pages(
            'pull_request_threads',
            "https://dev.azure.com/{}/{}/_apis/git/repositories/{}/pullrequests/{}/threads?" \
            "api-version={}" \
            .format(org, project, project_repo, prid, API_VERSION)
    ):
        threads = response.json()
        for thread in threads['value']:
            thread['_sdc_repository'] = "{}/{}/{}".format(org, project, project_repo)
            thread['_sdc_pullRequestId'] = prid
            with singer.Transformer() as transformer:
                rec = transformer.transform(thread, schema, metadata=metadata.to_map(mdata))
            yield rec

        # I'm honestly not sure what the purpose is of this, but it was in the github tap
        return state


def get_pull_request_heads(org, repo_path):
    reposplit = repo_path.split('/')
    project = reposplit[0]
    project_repo = reposplit[1]

    heads = {}

    for response in authed_get_all_pages(
            'pull_requests',
            "https://dev.azure.com/{}/{}/_apis/git/repositories/{}/pullrequests?" \
            "api-version={}&searchCriteria.status=all" \
            .format(org, project, project_repo, API_VERSION),
            '$top',
            '$skip',
            True # No link header to indicate availability of more data
    ):
        prs = response.json()['value']
        for pr in prs:
            prNumber = pr['pullRequestId']
            heads['refs/pull/{}/head'.format(prNumber)] = pr['lastMergeSourceCommit']['commitId']
            if 'lastMergeCommit' in pr:
                heads['refs/pull/{}/merge'.format(prNumber)] = pr['lastMergeCommit']['commitId']
    return heads

def get_all_pull_requests(schemas, org, repo_path, state, mdata, start_date):
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
                .format(org, project, project_repo, API_VERSION),
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

                prid = pr['pullRequestId']

                # List the PR commits to include those
                pr['commits'] = []
                for pr_commit_response in authed_get_all_pages(
                        'pull_requests/commits',
                        "https://dev.azure.com/{}/{}/_apis/git/repositories/{}/pullrequests/{}/commits?" \
                        "api-version={}" \
                        .format(org, project, project_repo, prid, API_VERSION),
                        '$top',
                        'continuationToken'
                ):
                    pr_commits = pr_commit_response.json()
                    pr['commits'].extend(pr_commits['value'])

                    # Note: These commits will already have their detail fetched by the commits
                    # endpoint (even if they are in an unmerged PR or abandoned), so we don't need
                    # to fetch more info here -- we only need to provide the shallow references.

                # Write out the pull request info

                pr['_sdc_repository'] = "{}/{}/{}".format(org, project, project_repo)

                # So pullRequestId isn't actually unique. There is a 'artifactId' parameter that is
                # unique, but, surprise surprise, the API doesn't actually include this property
                # when listing multiple PRs, so we need to construct it from the URL. Hilariously,
                # this ID also contains %2f for the later slashes instead of actual slashes.
                pr['artifactId'] = "vstfs:///Git/PullRequestId/{}%2f{}%2f{}" \
                    .format(pr['repository']['project']['id'], pr['repository']['id'], prid)

                with singer.Transformer() as transformer:
                    rec = transformer.transform(pr, schemas['pull_requests'], metadata=metadata.to_map(mdata))
                singer.write_record('pull_requests', rec, time_extracted=extraction_time)
                singer.write_bookmark(state, repo_path, 'pull_requests', {'since': singer.utils.strftime(extraction_time)})
                counter.increment()

                # sync pull_request_threads if that schema is present
                if schemas.get('pull_request_threads'):
                    for thread_rec in get_threads_for_pr(prid, schemas['pull_request_threads'], org, repo_path, state, mdata):
                        singer.write_record('pull_request_threads', thread_rec, time_extracted=extraction_time)
                        singer.write_bookmark(state, repo_path, 'pull_request_threads', {'since': singer.utils.strftime(extraction_time)})

    return state

def transform_repo_to_write(org, repo):
    project_name = repo['project']['name']
    repo_name = repo['name']

    return {
        '_sdc_repository': '{}/{}/{}'.format(org, project_name, repo_name),
        'id': '{}/{}/{}/{}'.format('azure-git', org, project_name, repo_name),
        'source': 'azure-git',
        'org_name': org,
        'repo_name': '{}/{}'.format(project_name, repo_name),
        'is_source_public': repo['project']['visibility'] == 'public',
        'default_branch': repo['defaultBranch'] if 'defaultBranch' in repo else '',
        'fork_org_name': None,
        'fork_repo_name': None,
        'description': repo['description'] if 'description' in repo else ''
    }

def get_all_repositories(schema, org, repo_path, state, mdata, start_date):
    # Don't bookmark this one for now
    extraction_time = singer.utils.now()

    repo_split = repo_path.split('/')
    project_name = repo_split[0]
    repo_name = repo_split[1]

    repos_to_write = []
    if not process_globals \
        and repo_path != '*/*' \
        and repo_path != '':
        repository_response = authed_get(
            'repository',
            "https://dev.azure.com/{}/{}/_apis/git/repositories/{}?api-version={}"
                .format(org, project_name, repo_name, API_VERSION)
        )
        repos_to_write.append(
            transform_repo_to_write(org, repository_response.json())
        )
    else:
        for response in authed_get_all_pages(
            'projects',
            "https://dev.azure.com/{}/_apis/projects?" \
            "api-version={}" \
            .format(org, API_VERSION),
            '$top',
            '$skip',
            True # No link header to indicate availability of more data
        ):
            projects = response.json()['value']
            for project in projects:
                projectName = project['name']
                # while _api/get/repositories is a "list" endpoint
                # it is not paginated. Using authed_get_all_pages will
                # cause the endpoint to be hit infinitely until azure-git cuts us off.
                repositories_response = authed_get(
                    'repositories',
                    "https://dev.azure.com/{}/{}/_apis/git/repositories?api-version={}"
                        .format(org, projectName, API_VERSION)
                )
                repos_to_write.extend(
                    [transform_repo_to_write(org, repo) \
                        for repo in repositories_response.json()['value'] \
                        if repo['isDisabled'] != True]
                )

    with metrics.record_counter('repositories') as counter:
        for repo_to_write in repos_to_write:
            with singer.Transformer() as transformer:
                rec = transformer.transform(repo_to_write, schema,
                    metadata=metadata.to_map(mdata))
            singer.write_record('repositories', rec, time_extracted=extraction_time)
            counter.increment()

    return state

def get_all_pipelines(schema, org, repo_path, state, mdata, start_date):
    ## NO BOOKMARKS
    with metrics.record_counter('pipelines') as counter:
        extraction_time = singer.utils.now()

        repo_split = repo_path.split('/')
        project = repo_split[0]
        repo_name = repo_split[1]

        repo_to_ingest = get_repository(org, project, repo_name)
        repo_id_to_ingest = repo_to_ingest["id"]

        # https://learn.microsoft.com/en-us/rest/api/azure/devops/pipelines/pipelines/list?view=azure-devops-rest-7.1
        for response in authed_get_all_pages(
            'pipelines_list',
            "https://dev.azure.com/{}/{}/_apis/pipelines?api-version={}" \
                .format(org, project, API_VERSION_7_1),
            '$top',
            'continuationToken'
        ):
            pipelines = response.json()['value']
            for pipeline_list_item in pipelines:
                # https://learn.microsoft.com/en-us/rest/api/azure/devops/pipelines/pipelines/get?view=azure-devops-rest-7.1
                pipeline_response = authed_get(
                    'pipeline',
                    "https://dev.azure.com/{}/{}/_apis/pipelines/{}?api-version={}" \
                        .format(org, project, pipeline_list_item['id'], API_VERSION_7_1)
                )
                raw_pipeline = pipeline_response.json()

                pipeline_repo = raw_pipeline \
                    .get('configuration', {}) \
                    .get('repository', {}) \
                    .get('id', None)

                # Currently, no way to filter pipelines for a specific repostory with the api
                # have to filter out pipelines not associated with this repo
                if pipeline_repo != repo_id_to_ingest:
                    logger.info(
                        "Ignoring pipeline {} because configuration repository is {}, not {} ({})".format(raw_pipeline["id"], pipeline_repo, repo_path, repo_id_to_ingest)
                    )
                    continue

                sdc_repository = '{}/{}/{}'.format(org, project, repo_name)
                pipeline = {
                    **raw_pipeline,
                    '_sdc_repository': sdc_repository,
                    '_sdc_id': '{}/pipeline/{}'.format(sdc_repository, raw_pipeline['id'])
                }

                with singer.Transformer() as transformer:
                    rec = transformer.transform(pipeline, schema,
                        metadata=metadata.to_map(mdata))
                singer.write_record('pipelines', rec, time_extracted=extraction_time)
                counter.increment()

                state = get_all_pipeline_runs(schema, org, repo_path, pipeline, state, mdata, start_date)

    return state

def get_all_pipeline_runs(schema, org, repo_path, pipeline, state, mdata, start_date):
    bookmark_key = "{}_finishedDate".format(pipeline['id'])
    bookmark = get_bookmark(state, repo_path, "pipeline", bookmark_key, start_date)
    if not bookmark:
        bookmark = '1970-01-01T00:00:00Z'

    bookmarkTime = parser.parse(bookmark)
    if bookmarkTime.tzinfo is None:
        bookmarkTime = pytz.UTC.localize(bookmarkTime)

    with metrics.record_counter('runs') as counter:
        extraction_time = singer.utils.now()

        repo_split = repo_path.split('/')
        project = repo_split[0]
        repo_name = repo_split[1]

        # https://learn.microsoft.com/en-us/rest/api/azure/devops/pipelines/runs/list?view=azure-devops-rest-7.1
        runs_response = authed_get(
            'runs',
            "https://dev.azure.com/{}/{}/_apis/pipelines/{}/runs?api-version={}" \
                .format(org, project, pipeline['id'], API_VERSION_7_1)
        )
        runs = runs_response.json()['value']
        for run_list_item in runs:
            if run_list_item.get('finishedDate', None):
                run_finished_date = parser.parse(run_list_item['finishedDate'])
                logger.info(run_finished_date)
                logger.info(bookmarkTime)
                if run_finished_date <= bookmarkTime:
                    continue

            # https://learn.microsoft.com/en-us/rest/api/azure/devops/pipelines/runs/get?view=azure-devops-rest-7.1
            run_response = authed_get(
                'run',
                "https://dev.azure.com/{}/{}/_apis/pipelines/{}/runs/{}?api-version={}" \
                    .format(org, project, pipeline['id'], run_list_item['id'], API_VERSION_7_1)
            )

            raw_run = run_response.json()
            sdc_repository = '{}/{}/{}'.format(org, project, repo_name)
            run = {
                **raw_run,
                '_sdc_repository': sdc_repository,
                '_sdc_id': '{}/pipeline/{}/run/{}'.format(sdc_repository, pipeline['id'], raw_run['id']),
                '_sdc_parent_id': '{}/pipeline/{}'.format(sdc_repository, pipeline['id'])
            }

            with singer.Transformer() as transformer:
                rec = transformer.transform(run, schema, metadata=metadata.to_map(mdata))
            singer.write_record('runs', rec, time_extracted=extraction_time)
            counter.increment()

        singer.write_bookmark(state, repo_path, 'pipeline', { bookmark_key: singer.utils.strftime(extraction_time) })

    return state

def is_build_after_time(build, time_to_compare):
    return 'finishedDate' not in build or \
        parser.parse(build['finishedDate']) > time_to_compare

def get_build_timeline(schema, org, repo_path, build, state, mdata, start_date):
    stream_id = 'build_timelines'

    with metrics.record_counter(stream_id) as counter:
        extraction_time = singer.utils.now()

        repo_split = repo_path.split('/')
        project = repo_split[0]
        repo_name = repo_split[1]

        # https://learn.microsoft.com/en-us/rest/api/azure/devops/build/timeline/get?view=azure-devops-rest-7.1
        build_timeline_response = authed_get(
            stream_id,
            "https://dev.azure.com/{}/{}/_apis/build/builds/{}/Timeline?api-version={}" \
                .format(org, project, build['id'], API_VERSION_7_1)
        )

        # if the build timeline is not available, azure-git returns an empty response
        if build_timeline_response.status_code == 204:
            logger.warn('Build timeline unavailable for {}/{} {}'.format(org, project, build['id']))
            return state

        raw_build_timeline = build_timeline_response.json()
        sdc_repository = '{}/{}/{}'.format(org, project, repo_name)

        build_timeline = {
            **raw_build_timeline,
            '_sdc_repository': sdc_repository,
            '_sdc_id': '{}/build/{}/timeline/{}'.format(sdc_repository, build['id'], raw_build_timeline['id']),
            '_sdc_parent_id': '{}/build/{}'.format(sdc_repository, build['id']),
        }

        with singer.Transformer() as transformer:
            rec = transformer.transform(build_timeline, schema, metadata=metadata.to_map(mdata))
        singer.write_record(stream_id, rec, time_extracted=extraction_time)
        counter.increment()

    return state

def get_all_builds(schema, org, repo_path, state, mdata, start_date):
    stream_id = "builds"
    bookmark_key = "finishedDate"

    bookmark = get_bookmark(state, repo_path, stream_id, bookmark_key, start_date)
    if not bookmark:
        bookmark = '1970-01-01T00:00:00Z'

    bookmarkTime = parser.parse(bookmark)
    if bookmarkTime.tzinfo is None:
        bookmarkTime = pytz.UTC.localize(bookmarkTime)

    with metrics.record_counter(stream_id) as counter, \
        singer.Transformer() as transformer:
        extraction_time = singer.utils.now()

        repo_split = repo_path.split('/')
        project = repo_split[0]
        repo_name = repo_split[1]

        repo_to_ingest = get_repository(org, project, repo_name)
        repo_id_to_ingest = repo_to_ingest["id"]

        # https://learn.microsoft.com/en-us/rest/api/azure/devops/build/builds/list?view=azure-devops-rest-7.1
        for response in authed_get_all_pages(
            stream_id,
            'https://dev.azure.com/{}/{}/_apis/build/builds?queryOrder={}&repositoryId={}&repositoryType={}&api-version={}' \
                .format(org, project, 'finishTimeDescending', repo_id_to_ingest, "TfsGit", API_VERSION_7_1),
            '$top',
            'continuationToken'
        ):
            builds = response.json()['value']
            sdc_repository = '{}/{}/{}'.format(org, project, repo_name)
            builds_to_write = [
                {
                    **build,
                    '_sdc_repository': sdc_repository,
                    '_sdc_id': '{}/build/{}'.format(sdc_repository, build['id'])
                }
                for build in builds
                if is_build_after_time(build, bookmarkTime)
            ]

            for build in builds_to_write:
                rec = transformer.transform(build, schema,
                        metadata=metadata.to_map(mdata))
                singer.write_record(stream_id, rec, time_extracted=extraction_time)
                counter.increment()

                # sync build_timeline substream
                state = get_build_timeline(schema, org, repo_path, build, state, mdata, start_date)

            # the API is ordered, as soon as we find an item that finished before our bookmark, stop iterating
            if len(builds_to_write) < len(builds):
                break

        singer.write_bookmark(state, repo_path, stream_id, {
            bookmark_key: singer.utils.strftime(extraction_time)
        })

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
    'commit_files': get_all_commit_files,
    'commit_files_meta': get_all_commit_files,
    'pull_requests': get_all_pull_requests,
    'repositories': get_all_repositories,
    'builds': get_all_builds,
    'pipelines': get_all_pipelines
}

SUB_STREAMS = {
    'pull_requests': ['pull_request_threads'],
    'commit_files': ['refs', 'annotated_tags'],
    'builds': ['build_timelines'],
    'pipelines': ['runs']
}

def do_sync(config, state, catalog):
    global process_globals
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
    process_globals = config['process_globals'] if 'process_globals' in config else True

    start_date = config['start_date'] if 'start_date' in config else None
    # get selected streams, make sure stream dependencies are met
    selected_stream_ids = get_selected_streams(catalog)
    validate_dependencies(selected_stream_ids)

    org = config['org']

    repositories = [config['repository']] if isinstance(config['repository'], str) else config['repository']


    domain = config['pull_domain'] if 'pull_domain' in config else 'dev.azure.com'
    gitLocal = GitLocal({
        'access_token': config['access_token'],
        'workingDir': '/tmp',
    }, 'https://{}@' + domain + '/{}', # repo is format: {org}/{project}/{repo}
        config['hmac_token'] if 'hmac_token' in config else None,
        logger,
        commitsOnly='commit_files_meta' in selected_stream_ids)

    processed_repositories = False
    #pylint: disable=too-many-nested-blocks
    for repo in repositories:
        logger.info("Starting sync of repository: %s", repo)
        for stream in catalog['streams']:
            stream_id = stream['tap_stream_id']
            stream_schema = stream['schema']
            mdata = stream['metadata']

            if stream_id == 'repositories':
                # Only load this once, and only if process_globals is set to true
                if processed_repositories:
                    continue
                processed_repositories = True

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
                    if stream_id == 'commit_files' or stream_id == 'commit_files_meta':
                        heads = get_pull_request_heads(org, repo)
                        # We don't need to also get open branch heads here becuase those are
                        # included in the git clone --mirror, though PR heads for merged PRs are
                        # not included.
                        commits_only = stream_id == 'commit_files_meta'
                        state = sync_func(stream_schemas, org, repo, state, mdata, start_date,
                            gitLocal, heads, commits_only)
                    else:
                        state = sync_func(stream_schemas, org, repo, state, mdata, start_date)

    # The state can get big, don't write it until the end
    singer.write_state(state)

@singer.utils.handle_top_exception(logger)
def main():
    args = singer.utils.parse_args(REQUIRED_CONFIG_KEYS)

    # Initialize basic auth
    user_name = args.config['user_name']
    access_token = args.config['access_token']
    logger.addToken(access_token)
    session.auth = (user_name, access_token)

    if args.discover:
        do_discover(args.config)
    else:
        catalog = args.properties if args.properties else get_catalog()
        do_sync(args.config, args.state, catalog)

if __name__ == '__main__':
    main()
