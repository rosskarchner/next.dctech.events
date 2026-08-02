"""Newsletter signup/confirm Lambda for next.dctech.events.

Port of the live dctech-newsletter Chalice app's exact mechanics — double
opt-in, KMS HMAC_SHA_512-signed confirmation links, CSRF secret from Secrets
Manager, SESv2 contact list/topic — into a plain Lambda-proxy router (no
Chalice), against this stack's own isolated SES/KMS/Secrets resources.
"""
import hashlib
import hmac
import json
import os
import uuid
from base64 import urlsafe_b64decode, urlsafe_b64encode
from datetime import datetime, timezone
from urllib.parse import parse_qs

import boto3
from jinja2 import Environment, FileSystemLoader, select_autoescape

env = Environment(
    loader=FileSystemLoader(os.path.join(os.path.dirname(__file__), 'templates')),
    autoescape=select_autoescape(['html', 'xml']),
)

ses = boto3.client('sesv2')
kms = boto3.client('kms')
secrets = boto3.client('secretsmanager')

FROM_EMAIL = os.environ.get('FROM_EMAIL', 'outbound@dctech.events')
REPLY_TO_EMAIL = os.environ.get('REPLY_TO_EMAIL', 'ross@karchner.com')
CONTACT_LIST_NAME = os.environ.get('CONTACT_LIST_NAME', 'newsletters')
TOPIC_NAME = os.environ.get('TOPIC_NAME', 'dctech')
CONFIRMATION_KEY_ID = os.environ.get('CONFIRMATION_KEY_ID')
CSRF_SECRET_NAME = os.environ.get('CSRF_SECRET_NAME', 'dctech-events-next/newsletter-csrf')
BASE_URL = os.environ.get('BASE_URL', '')  # public URL of this API, no trailing slash
# When served through the CloudFront /newsletter* behavior, API Gateway sees
# paths that still carry this prefix; strip it before routing.
PATH_PREFIX = os.environ.get('PATH_PREFIX', '').rstrip('/')
CSRF_SECRET = None

# The one newsletter this stack serves, on the long-standing SES contact
# list and topic so existing subscribers carry over.
NEWSLETTERS = {
    'dctech': {
        'contact_list_name': CONTACT_LIST_NAME,
        'topic_name': TOPIC_NAME,
    },
}
DEFAULT_NEWSLETTERS = ['dctech']


def _now_ts():
    return datetime.now(timezone.utc).timestamp()


def filter_signup_newsletters(newsletters):
    """Return newsletter slugs that currently accept new subscriptions."""
    # Accept the retired parallel-stack slug so any stale form still works.
    normalized = ['dctech' if n == 'dctech-next' else n for n in newsletters]
    return [slug for slug in normalized if slug in NEWSLETTERS]


def get_csrf_secret():
    global CSRF_SECRET
    if CSRF_SECRET is None:
        response = secrets.get_secret_value(SecretId=CSRF_SECRET_NAME)
        CSRF_SECRET = json.loads(response['SecretString'])['csrf_secret']
    return CSRF_SECRET


def generate_csrf_token(confirmation_id):
    """Generate a CSRF token for form protection"""
    secret = get_csrf_secret()
    timestamp = str(int(_now_ts()))
    message = f"{confirmation_id}:{timestamp}".encode()
    signature = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    return f"{timestamp}.{signature}"


def verify_csrf_token(confirmation_id, token):
    """Verify a CSRF token for form protection and check it hasn't expired"""
    try:
        timestamp, signature = token.split('.')
        message = f"{confirmation_id}:{timestamp}".encode()
        secret = get_csrf_secret()
        expected_signature = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()

        if int(_now_ts()) - int(timestamp) > 3600:  # 1 hour
            return False

        return hmac.compare_digest(signature, expected_signature)
    except Exception:
        return False


def generate_confirmation_signature(email, timestamp, newsletters=None):
    """Generate a KMS HMAC signature for email confirmation"""
    if newsletters is None:
        newsletters = DEFAULT_NEWSLETTERS
    newsletters_str = ','.join(sorted(newsletters))
    message = f"{email}:{newsletters_str}:{timestamp}".encode()
    response = kms.generate_mac(
        Message=message,
        KeyId=CONFIRMATION_KEY_ID,
        MacAlgorithm='HMAC_SHA_512',
    )
    return urlsafe_b64encode(response['Mac']).decode('utf-8').rstrip('=')


def verify_confirmation_signature(email, timestamp, signature, newsletters=None):
    """Verify a KMS HMAC signature for email confirmation"""
    try:
        if newsletters is None:
            newsletters = DEFAULT_NEWSLETTERS
        if not CONFIRMATION_KEY_ID:
            raise ValueError('Missing CONFIRMATION_KEY_ID configuration')

        newsletters_str = ','.join(sorted(newsletters))
        message = f"{email}:{newsletters_str}:{timestamp}".encode()
        padded_sig = signature + '=' * (-len(signature) % 4)
        try:
            mac_bytes = urlsafe_b64decode(padded_sig.encode())
            response = kms.verify_mac(
                Message=message,
                KeyId=CONFIRMATION_KEY_ID,
                MacAlgorithm='HMAC_SHA_512',
                Mac=mac_bytes,
            )
            return response['MacValid']
        except Exception as e:
            print(f"Error in base64 decoding or KMS verification: {e}")
            return False
    except Exception as e:
        print(f"Error verifying signature: {e}")
        return False


def generate_confirmation_url(email, newsletters=None):
    """Generate a signed confirmation URL using path parameters and base64 encoded email"""
    if newsletters is None:
        newsletters = DEFAULT_NEWSLETTERS
    timestamp = int(_now_ts())
    signature = generate_confirmation_signature(email, timestamp, newsletters)
    encoded_email = urlsafe_b64encode(email.encode()).decode('utf-8').rstrip('=')
    encoded_timestamp = urlsafe_b64encode(str(timestamp).encode()).decode('utf-8').rstrip('=')
    newsletters_param = ','.join(sorted(newsletters))
    return f"/confirm/{encoded_email}/{encoded_timestamp}/{signature}?newsletters={newsletters_param}"


# ─── HTTP helpers ─────────────────────────────────────────────────

CORS_HEADERS = {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Headers': 'Content-Type,HX-Request,HX-Trigger,HX-Target,HX-Prompt,HX-Current-URL,HX-Boosted,HX-History-Restore-Request',
    'Access-Control-Allow-Methods': 'GET,POST,OPTIONS',
}


def _html(body, status=200, extra_headers=None):
    headers = {'Content-Type': 'text/html; charset=utf-8',
               'X-Robots-Tag': 'noindex, nofollow', **CORS_HEADERS}
    if extra_headers:
        headers.update(extra_headers)
    return {'statusCode': status, 'headers': headers, 'body': body}


def _json(body, status=200):
    return {'statusCode': status,
            'headers': {'Content-Type': 'application/json', **CORS_HEADERS},
            'body': json.dumps(body)}


def _render(name, **ctx):
    ctx.setdefault('base_path', BASE_URL)
    return env.get_template(name).render(**ctx)


def _is_htmx(event):
    headers = {k.lower(): v for k, v in (event.get('headers') or {}).items()}
    return headers.get('hx-request') == 'true'


def _parse_form(event):
    body = event.get('body') or ''
    return parse_qs(body)


# ─── Routes ───────────────────────────────────────────────────────

def route_index(event):
    temp_id = str(uuid.uuid4())
    csrf_token = generate_csrf_token(temp_id)
    template = 'partials/signup_form.html' if _is_htmx(event) else 'index.html'
    return _html(_render(template, csrf_token=csrf_token, temp_id=temp_id))


def route_signup(event):
    is_htmx = _is_htmx(event)
    headers = {k.lower(): v for k, v in (event.get('headers') or {}).items()}
    content_type = headers.get('content-type', '')

    try:
        if content_type.startswith('application/x-www-form-urlencoded'):
            data = _parse_form(event)
            email = data.get('email', [''])[0]
            newsletters = data.get('newsletters', DEFAULT_NEWSLETTERS)
        else:
            body = json.loads(event.get('body') or '{}')
            email = body.get('email', '')
            newsletters = body.get('newsletters', DEFAULT_NEWSLETTERS)
            if isinstance(newsletters, str):
                newsletters = [newsletters]
    except Exception:
        email = ''
        newsletters = DEFAULT_NEWSLETTERS

    if not email:
        error_msg = 'Email is required'
        if is_htmx:
            return _html(_render('partials/error.html', error=error_msg), 400)
        return _json({'error': error_msg}, 400)

    newsletters = filter_signup_newsletters(newsletters)
    if not newsletters:
        error_msg = 'Please select an available newsletter'
        if is_htmx:
            return _html(_render('partials/error.html', error=error_msg), 400)
        return _json({'error': error_msg}, 400)

    try:
        if not CONFIRMATION_KEY_ID:
            raise ValueError('Missing CONFIRMATION_KEY_ID configuration')

        confirmation_url = generate_confirmation_url(email, newsletters)
        full_confirmation_url = f"{BASE_URL}{confirmation_url}"

        html_content = _render('confirmation_email.html',
                               confirmation_url=full_confirmation_url)
        ses.send_email(
            FromEmailAddress=FROM_EMAIL,
            ReplyToAddresses=[REPLY_TO_EMAIL],
            Destination={'ToAddresses': [email]},
            Content={
                'Simple': {
                    'Subject': {'Data': 'Confirm your subscription to DCTech Events Newsletter'},
                    'Body': {'Html': {'Data': html_content}},
                }
            },
        )

        success_msg = ('Please check your email (and maybe your junk folder!) '
                       'to confirm your subscription.')
        if is_htmx:
            return _html(_render('partials/success.html', message=success_msg))
        return _json({'message': success_msg})

    except Exception as e:
        print(f"Subscription error: {e}")
        error_msg = 'Failed to process subscription'
        if is_htmx:
            return _html(_render('partials/error.html', error=error_msg), 500)
        return _json({'error': error_msg}, 500)


def route_confirm_link(event, encoded_email, encoded_timestamp, signature):
    try:
        query_params = event.get('queryStringParameters') or {}
        newsletters_param = query_params.get('newsletters', 'dctech')
        newsletters = [n.strip() for n in newsletters_param.split(',')]

        padded_email = encoded_email + '=' * (-len(encoded_email) % 4)
        padded_timestamp = encoded_timestamp + '=' * (-len(encoded_timestamp) % 4)
        email = urlsafe_b64decode(padded_email.encode()).decode('utf-8')
        timestamp = int(urlsafe_b64decode(padded_timestamp.encode()).decode('utf-8'))

        if _now_ts() - timestamp > 21600:  # 6 hours
            return _html(_render('error.html', error='Confirmation link has expired'), 400)

        if not verify_confirmation_signature(email, timestamp, signature, newsletters):
            return _html(_render('error.html', error='Invalid confirmation link'), 400)

        return _html(_render('confirm.html',
                             email=email,
                             timestamp=timestamp,
                             signature=signature,
                             newsletters=','.join(newsletters)))
    except Exception as e:
        print(f"Error in confirmation: {e}")
        return _html(_render('error.html', error='Invalid confirmation link'), 400)


def route_confirm_post(event):
    try:
        data = _parse_form(event)
        email = data.get('email', [''])[0]
        timestamp = data.get('timestamp', [''])[0]
        signature = data.get('signature', [''])[0]
        newsletters_str = data.get('newsletters', ['dctech'])[0]
        newsletters = [n.strip() for n in newsletters_str.split(',')]

        if not all([email, timestamp, signature]):
            return _html(_render('error.html', error='Invalid confirmation data'), 400)

        timestamp = int(timestamp)
        if _now_ts() - timestamp > 21600:
            return _html(_render('error.html', error='Confirmation link has expired'), 400)

        if not verify_confirmation_signature(email, timestamp, signature, newsletters):
            return _html(_render('error.html', error='Invalid confirmation data'), 400)

        newsletters = filter_signup_newsletters(newsletters)
        if not newsletters:
            return _html(_render('error.html',
                                 error='Selected newsletters are no longer accepting subscriptions'), 400)

        for newsletter_slug in newsletters:
            newsletter_config = NEWSLETTERS[newsletter_slug]
            contact_list_name = newsletter_config['contact_list_name']
            topic_name = newsletter_config['topic_name']

            try:
                ses.create_contact(
                    ContactListName=contact_list_name,
                    EmailAddress=email,
                    TopicPreferences=[{'TopicName': topic_name,
                                       'SubscriptionStatus': 'OPT_IN'}],
                )
            except ses.exceptions.AlreadyExistsException:
                contact = ses.get_contact(
                    ContactListName=contact_list_name, EmailAddress=email)
                existing_preferences = contact['TopicPreferences']
                topic_found = False
                for pref in existing_preferences:
                    if pref['TopicName'] == topic_name:
                        pref['SubscriptionStatus'] = 'OPT_IN'
                        topic_found = True
                        break
                if not topic_found:
                    existing_preferences.append({'TopicName': topic_name,
                                                 'SubscriptionStatus': 'OPT_IN'})
                ses.update_contact(
                    ContactListName=contact_list_name,
                    EmailAddress=email,
                    TopicPreferences=existing_preferences,
                )

        return {'statusCode': 303, 'body': '',
                'headers': {'Location': f'{BASE_URL}/confirm/success',
                            'Content-Type': 'text/html'}}
    except Exception as e:
        print(e)
        return _html(_render('error.html', error='Failed to confirm subscription'), 500)


def route_confirm_success(event):
    return _html(_render('success.html'))


def lambda_handler(event, context):
    # BASE_URL can't be an env var (Lambda↔API circular reference in CFN),
    # so derive the public URL from the incoming request when unset.
    global BASE_URL
    if not os.environ.get('BASE_URL'):
        rc = event.get('requestContext', {}) or {}
        domain = rc.get('domainName')
        stage = rc.get('stage')
        if domain:
            BASE_URL = f'https://{domain}/{stage}' if stage else f'https://{domain}'

    method = event.get('httpMethod', 'GET')
    path = event.get('path', '/') or '/'
    if PATH_PREFIX and path.startswith(PATH_PREFIX):
        path = path[len(PATH_PREFIX):] or '/'

    if method == 'OPTIONS':
        return {'statusCode': 200, 'headers': CORS_HEADERS, 'body': ''}

    parts = [p for p in path.split('/') if p]

    if method == 'GET' and path in ('/', ''):
        return route_index(event)
    if method == 'POST' and path == '/signup':
        return route_signup(event)
    if method == 'GET' and path == '/confirm/success':
        return route_confirm_success(event)
    if method == 'GET' and len(parts) == 4 and parts[0] == 'confirm':
        return route_confirm_link(event, parts[1], parts[2], parts[3])
    if method == 'POST' and path == '/confirm':
        return route_confirm_post(event)

    return _json({'error': 'Not found'}, 404)
