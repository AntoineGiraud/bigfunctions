import time
import re
import json
import datetime
import traceback

from flask import Flask, request, jsonify
import google.auth
import google.cloud.error_reporting


error_reporter = google.cloud.error_reporting.Client()
app = Flask(__name__)


CACHE = {}
CURRENT_LOCATION = '{{ cloud_run_location }}'

QUOTAS = {{ quotas if quotas is defined else {} }}


_, PROJECT = google.auth.default()


def get_current_service_account():
    if 'current_service_account' not in CACHE:
        import urllib.request
        url = 'http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/email'
        req = urllib.request.Request(url)
        req.add_header('Metadata-Flavor', 'Google')
        with urllib.request.urlopen(req) as f:
            CACHE['current_service_account'] = f.read().decode('utf-8')
    return CACHE['current_service_account']


class QuotaException(Exception):
    pass



class Logger:

    def __init__(self, data):
        self.created_time = time.time()
        self.user = data['sessionUser']
        self.row_count = len(data['calls'])
        self.request_id = data['requestId']
        self.caller = data['caller']

    def log(self, **kwargs):
        duration = 1000 * (time.time() - self.created_time)
        status = kwargs['status']
        message = {
            **{
                "severity": 'INFO',
                'message': f"{status.upper()}: {{ name }} from {self.user} with {self.row_count} rows (elasped {duration} ms)",
                "bigfunction": '{{ name }}',
                "user": self.user,
                "row_count": self.row_count,
                "request_id": self.request_id,
                "caller": self.caller,
                'elapsed_ms': duration,
            },
            **kwargs,
        }
        print(json.dumps(message))


class BaseQuotaManager:

    def __init__(self, data):
        self.row_count = len(data['calls'])

    def check_quotas(self):
        if 'max_rows_per_query' in QUOTAS and (self.row_count > QUOTAS['max_rows_per_query']):
            raise QuotaException(f"It only accepts {QUOTAS['max_rows_per_query']} rows per query and you called it now on {self.row_count} rows or more.")


class DatastoreQuotaManager(BaseQuotaManager):

    kind = 'bigfunction_call'

    _datastore = None

    def __init__(self, data):
        super().__init__(data)
        self.created_at = datetime.datetime.utcnow()
        self.user = data['sessionUser']
        today = self.created_at.strftime("%Y-%m-%d")
        self.user_bigfunction_date = f'{self.user}/{{ name }}/{today}'

    @property
    def datastore(self):
        if self._datastore is None:
            import google.cloud.datastore
            self._datastore = google.cloud.datastore.Client()
        return self._datastore

    def compute_stat(self, aggregate_function, aggregate_attribute=None, filter=None):
        from google.cloud.datastore import query as filters
        aggregate_attributes = aggregate_attribute or []
        filter = filter or []
        query = self.datastore.query(kind=self.kind)
        if filter:
            query.add_filter(filter=filters.PropertyFilter(*filter))
        result = getattr(self.datastore.aggregation_query(query), aggregate_function)(aggregate_attribute).fetch()
        result = list(list(result)[0])[0].value
        print(f'{aggregate_function}({aggregate_attributes}) where {"".join(filter)}:', result)
        return result

    def get_today_row_count_for_this_bigfunction(self):
        return self.compute_stat('sum', aggregate_attribute='row_count', filter=["user_bigfunction_date", "=", self.user_bigfunction_date])

    def save_usage(self):
        import google.cloud.datastore
        key = self.datastore.key(self.kind)
        entity = google.cloud.datastore.Entity(key)
        entity.update({
            'timestamp': self.created_at,
            'user_bigfunction_date': self.user_bigfunction_date,
            "row_count": self.row_count,
        })
        self.datastore.put(entity)

    def check_quotas(self):
        super().check_quotas()
        if 'max_rows_per_user_per_day' in QUOTAS:
            today_row_count_for_this_bigfunction = self.get_today_row_count_for_this_bigfunction()
            if today_row_count_for_this_bigfunction + self.row_count > QUOTAS['max_rows_per_user_per_day']:
                raise QuotaException(f"It only accepts {QUOTAS['max_rows_per_user_per_day']} rows per day per user and you called it today for {today_row_count_for_this_bigfunction + self.row_count} rows.")
            self.save_usage()


QuotaManager = DatastoreQuotaManager if QUOTAS.get('backend') == 'datastore' else BaseQuotaManager


class SecretManager:

    secret_manager = None
    secrets = {}

    def get(self, name):
        if name in self.secrets:
            return self.secrets[name]
        if self.secret_manager is None:
            import google.cloud.secretmanager
            self.secret_manager = google.cloud.secretmanager.SecretManagerServiceClient()
        self.secrets[name] = self.secret_manager.access_secret_version(
            name=f'projects/{PROJECT}/secrets/{name}/versions/latest'
        ).payload.data.decode('UTF-8')
        return self.secrets[name]


secrets = SecretManager()
{% if secrets is defined %}
{% for secret in secrets %}
{{ secret.name }} = secrets.get('{{ secret.name }}')
{% endfor %}
{% endif %}


{{ init_code }}

{% if code_process_rows_as_batch %}

def compute_all_rows(rows, bigfunction_user, user_project):
    {{ code | replace('\n', '\n    ') | replace('{BIGFUNCTIONS_DATASET}',  '`' +  project + '`.`' + dataset + '`' ) }}

{% else %}

def compute_one_row(args, bigfunction_user, user_project):
    {% if arguments %}{% for argument in arguments %}{{ argument.name }}, {% endfor %} = args{% endif %}
    {{ code | replace('\n', '\n    ') | replace('{BIGFUNCTIONS_DATASET}',  '`' +  project + '`.`' + dataset + '`' ) }}

{% endif %}


@app.route("/", methods=['POST'])
def handle():
    try:
        data = request.get_json()
        logger = Logger(data)
        logger.log(status='started')
        user_project_matches = re.findall(r'bigquery.googleapis.com/projects/([^/]*)/', data['caller'])
        user_project = user_project_matches[0] if user_project_matches else None
        bigfunction_user = data['sessionUser']
        quota_manager = QuotaManager(data)
        quota_manager.check_quotas()
        rows = data['calls']
        {% if code_process_rows_as_batch %}
        replies = compute_all_rows(rows, bigfunction_user, user_project)
        {% else %}
        replies = [compute_one_row(row, bigfunction_user, user_project) for row in rows]
        {% endif %}
        response = jsonify( { "replies" :  replies} )
        logger.log(status='success')
        return response
    except QuotaException as e:
        error_message = e.args[0]
        logger.log(status='quota_error', status_info=error_message)
        return jsonify({
            'errorMessage': f'''
                Thanks for using BigFunctions!
                The use of this function `{{ name }}` is limited by quotas.
                {error_message}.
                To remove this limit, you can ask for quotas increase to `{QUOTAS['contact']}` or deploy the function in your own project.
                Details are here: https://github.com/unytics/bigfunctions
                If you need help, please reach out to the slack: https://join.slack.com/t/bigfunctions/shared_invite/zt-1gbv491mu-cs03EJbQ1fsHdQMcFN7E1Q
            '''.replace('\n', ' ').replace('  ', ' ').replace('  ', ' '),
        }), 400
    except AssertionError as e:
        error_message = e.args[0]
        logger.log(status='assertion_error', status_info=error_message)
        return jsonify({'errorMessage': error_message}), 400
    except Exception as e:
        error_message = (str(e) + ' --- ' + traceback.format_exc())[:1000]
        logger.log(status='error', status_info=error_message)
        error_reporter.report_exception(google.cloud.error_reporting.build_flask_context(request))
        return jsonify({'errorMessage': f"{type(e).__name__}: {str(e)}"}), 400
