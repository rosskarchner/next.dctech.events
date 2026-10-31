"""Newsletter signup/confirm Lambda for next.dctech.events.

Port of the live dctech-newsletter Chalice app's mechanics — double
opt-in, KMS HMAC_SHA_512-signed confirmation links, SESv2 contact
list/topic — into a plain Lambda-proxy router (no Chalice), against this
stack's own isolated SES/KMS resources.

The form used to carry a CSRF token. It was never verified, and couldn't
have protected anything: it wasn't tied to a session, so a script could
fetch a fresh one from GET /newsletter/, and signup has no session to
forge. Abuse is bounded by route_signup's per-address rate limit instead.
"""
import json
import os
from base64 import urlsafe_b64decode, urlsafe_b64encode
from datetime import datetime, timezone
from urllib.parse import parse_qs

import boto3
from jinja2 import Environment, FileSystemLoader, select_autoescape

import db
import magic_link
from constants import FROM_EMAIL, REPLY_TO_EMAIL, CONTACT_LIST_NAME

env = Environment(
    loader=FileSystemLoader(os.path.join(os.path.dirname(__file__), 'templates')),
    autoescape=select_autoescape(['html', 'xml']),
)

ses = boto3.client('sesv2')
kms = boto3.client('kms')

TOPIC_NAME = os.environ.get('TOPIC_NAME', 'dctech')
CONFIRMATION_KEY_ID = os.environ.get('CONFIRMATION_KEY_ID')
BASE_URL = os.environ.get('BASE_URL', '')  # public URL of this API, no trailing slash
# When served through the CloudFront /newsletter* behavior, API Gateway sees
# paths that still carry this prefix; strip it before routing.
PATH_PREFIX = os.environ.get('PATH_PREFIX', '').rstrip('/')
# The public site root, where /edit/preferences.html lives. BASE_URL above
# is this Lambda's own /newsletter mount, which is the wrong place for a
# preferences link.
SITE_URL = os.environ.get('SITE_URL', '').rstrip('/')
PREFERENCES_PATH = '/edit/preferences.html'

# One message for both branches of route_signup, so the response never
# reveals whether an address is already on the list.
SIGNUP_SENT_MESSAGE = ('Check your email (and maybe your junk folder!) for a '
                       'link to confirm your subscription or manage your '
                       'preferences.')

# The one newsletter this stack serves, on the long-standing SES contact
# list and topic so existing subscribers carry over.
NEWSLETTERS = {
    'dctech': {
        'contact_list_name': CONTACT_LIST_NAME,
        'topic_name': TOPIC_NAME,
    },
}
DEFAULT_NEWSLETTERS = ['dctech']

# Static and effectively permanent — same list, same reasoning, as
# routes/preferences.py in the API Lambda (see that module's own comment):
# no DynamoDB equivalent exists, site/regions.py is the source of truth.
# Update all three places if a region is ever added.
REGIONS = {
    'dc': {'slug': 'dc', 'name': 'Washington DC'},
    'md': {'slug': 'md', 'name': 'Maryland'},
    'va': {'slug': 'va', 'name': 'Virginia'},
}


def _clean_slugs(values, valid_slugs):
    """Normalize a categories/regions selection to a deduped list of only
    real, currently-existing slugs — matching routes/preferences.py's own
    _clean_slugs (not shared code, but the same invariant: a stale or
    hand-crafted slug is dropped rather than stored)."""
    if not values:
        return []
    if isinstance(values, str):
        values = [values]
    seen = []
    for v in values:
        v = str(v or '').strip()
        if v and v in valid_slugs and v not in seen:
            seen.append(v)
    return seen


def _now_ts():
    return datetime.now(timezone.utc).timestamp()


def filter_signup_newsletters(newsletters):
    """Return newsletter slugs that currently accept new subscriptions."""
    # Accept the retired parallel-stack slug so any stale form still works.
    normalized = ['dctech' if n == 'dctech-next' else n for n in newsletters]
    return [slug for slug in normalized if slug in NEWSLETTERS]


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


def generate_confirmation_url(email, newsletters=None, categories=None, regions=None):
    """Generate a signed confirmation URL using path parameters and base64
    encoded email.

    categories/regions ride along as plain, unsigned query params — unlike
    email/newsletters (covered by generate_confirmation_signature's
    message), they're not worth protecting the way those are: the worst a
    tampered value could do is set the *same, already-signature-verified*
    email's own category/region preferences to something else, not forge a
    subscription or impersonate anyone. Keeping them out of the signed
    message avoids changing that message's shape at all.
    """
    if newsletters is None:
        newsletters = DEFAULT_NEWSLETTERS
    timestamp = int(_now_ts())
    signature = generate_confirmation_signature(email, timestamp, newsletters)
    encoded_email = urlsafe_b64encode(email.encode()).decode('utf-8').rstrip('=')
    encoded_timestamp = urlsafe_b64encode(str(timestamp).encode()).decode('utf-8').rstrip('=')
    newsletters_param = ','.join(sorted(newsletters))
    url = (f"/confirm/{encoded_email}/{encoded_timestamp}/{signature}"
           f"?newsletters={newsletters_param}")
    if categories:
        url += f"&categories={','.join(categories)}"
    if regions:
        url += f"&regions={','.join(regions)}"
    return url


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


# ─── Subscribe-or-manage branching ────────────────────────────────

def _site_url():
    if SITE_URL:
        return SITE_URL
    if PATH_PREFIX and BASE_URL.endswith(PATH_PREFIX):
        return BASE_URL[:-len(PATH_PREFIX)]
    return BASE_URL


def _is_subscribed(email):
    """True only if the address is on the contact list, opted in to our
    topic, and not globally unsubscribed — the same test sender.py uses to
    decide who gets mail, plus UnsubscribeAll.

    Any lookup failure (NotFoundException for a new address, or a transient
    SES error) counts as not subscribed: the fallback is a double-opt-in
    confirmation email, which is harmless to send to someone who turns out
    to be subscribed already.
    """
    newsletter = NEWSLETTERS[DEFAULT_NEWSLETTERS[0]]
    try:
        contact = ses.get_contact(
            ContactListName=newsletter['contact_list_name'], EmailAddress=email)
    except Exception as e:
        if type(e).__name__ != 'NotFoundException':
            print(f"get_contact failed, treating as not subscribed: {e}")
        return False
    if contact.get('UnsubscribeAll'):
        return False
    return any(
        pref.get('TopicName') == newsletter['topic_name']
        and pref.get('SubscriptionStatus') == 'OPT_IN'
        for pref in contact.get('TopicPreferences', [])
    )


def _send_manage_link(email):
    timestamp, signature = magic_link.generate_token(email, purpose='prefs')
    link = magic_link.build_link(email, timestamp, signature,
                                 path=PREFERENCES_PATH, base_url=_site_url())
    days = magic_link.ttl_seconds_for('prefs') // 86400
    ses.send_email(
        FromEmailAddress=FROM_EMAIL,
        ReplyToAddresses=[REPLY_TO_EMAIL],
        Destination={'ToAddresses': [email]},
        Content={'Simple': {
            'Subject': {'Data': 'Manage your DC Tech Events newsletter subscription'},
            'Body': {
                'Html': {'Data': _render('manage_email.html', link=link, days=days)},
                'Text': {'Data': (
                    "You're already subscribed to the DC Tech Events "
                    "newsletter. Use this link to choose which topics and "
                    f"areas you hear about:\n\n{link}\n\n"
                    f"The link works for the next {days} days. If you did "
                    "not request it, you can ignore this email; nothing "
                    "was changed.\n"
                )},
            },
        }},
    )


# ─── Routes ───────────────────────────────────────────────────────

def route_index(event):
    template = 'partials/signup_form.html' if _is_htmx(event) else 'index.html'
    return _html(_render(template))


def _signup_error(is_htmx, error_msg, status, email=''):
    """An error from /signup. JSON callers get the real status. htmx gets
    200 with the form re-rendered around the message: htmx doesn't swap a
    4xx/5xx response in by default, so the user would otherwise click and
    see nothing — and the form (with their email kept) must survive, since
    the swap replaces it, on the homepage as well as on /newsletter/."""
    if is_htmx:
        return _html(_render('partials/signup_form.html', error=error_msg, email=email))
    return _json({'error': error_msg}, status)


def route_signup(event):
    is_htmx = _is_htmx(event)
    headers = {k.lower(): v for k, v in (event.get('headers') or {}).items()}
    content_type = headers.get('content-type', '')

    try:
        if content_type.startswith('application/x-www-form-urlencoded'):
            data = _parse_form(event)
            email = data.get('email', [''])[0]
            newsletters = data.get('newsletters', DEFAULT_NEWSLETTERS)
            categories = data.get('categories', [])
            regions = data.get('regions', [])
        else:
            body = json.loads(event.get('body') or '{}')
            email = body.get('email', '')
            newsletters = body.get('newsletters', DEFAULT_NEWSLETTERS)
            if isinstance(newsletters, str):
                newsletters = [newsletters]
            categories = body.get('categories', [])
            regions = body.get('regions', [])
    except Exception:
        email = ''
        newsletters = DEFAULT_NEWSLETTERS
        categories = []
        regions = []

    # Optional — a signup with no selections just means "everything",
    # same default as a subscriber who never visits the preferences page.
    categories = _clean_slugs(categories, set(db.get_all_categories().keys()))
    regions = _clean_slugs(regions, set(REGIONS.keys()))

    email = magic_link.normalize_email(email)
    if not email:
        error_msg = 'Email is required'
        return _signup_error(is_htmx, error_msg, 400, email)
    if not magic_link.is_valid_email(email):
        error_msg = 'Please enter a valid email address'
        return _signup_error(is_htmx, error_msg, 400, email)

    newsletters = filter_signup_newsletters(newsletters)
    if not newsletters:
        error_msg = 'Please select an available newsletter'
        return _signup_error(is_htmx, error_msg, 400, email)

    try:
        # Per-address cap on emails this site sends: the same MAGICLINK#
        # counter /api/submit-link uses (1/minute, 6/day), so signup,
        # manage, submit and correct links share one allowance per inbox.
        # The 429 says only that a link was sent recently, never whether
        # the address is subscribed.
        allowed, retry_after = db.check_and_record_link_request(email)
        if not allowed:
            minutes = max(1, round(retry_after / 60))
            error_msg = (f'A link was just sent to that address. Please check '
                         f'your inbox, or try again in {minutes} '
                         f'minute{"s" if minutes != 1 else ""}.')
            return _signup_error(is_htmx, error_msg, 429, email)

        # One entry point for both "subscribe" and "manage": someone already
        # on the list gets a preferences link, everyone else (new, or
        # previously unsubscribed) gets the double-opt-in confirmation,
        # where they choose categories/regions. The response is identical
        # either way.
        if _is_subscribed(email):
            _send_manage_link(email)
        else:
            if not CONFIRMATION_KEY_ID:
                raise ValueError('Missing CONFIRMATION_KEY_ID configuration')

            confirmation_url = generate_confirmation_url(
                email, newsletters, categories=categories, regions=regions)
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

        success_msg = SIGNUP_SENT_MESSAGE
        if is_htmx:
            return _html(_render('partials/success.html', message=success_msg))
        return _json({'message': success_msg})

    except Exception as e:
        print(f"Subscription error: {e}")
        error_msg = 'Failed to process subscription'
        return _signup_error(is_htmx, error_msg, 500, email)


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

        # Unsigned pre-selections for the confirm page's checkboxes — see
        # generate_confirmation_url's own docstring for why that's fine.
        all_categories = db.get_all_categories()
        categories = _clean_slugs(
            query_params.get('categories', '').split(','),
            set(all_categories.keys()))
        regions = _clean_slugs(
            query_params.get('regions', '').split(','), set(REGIONS.keys()))

        return _html(_render('confirm.html',
                             email=email,
                             timestamp=timestamp,
                             signature=signature,
                             newsletters=','.join(newsletters),
                             all_categories=all_categories,
                             all_regions=REGIONS,
                             selected_categories=categories,
                             selected_regions=regions))
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
        # Checkboxes arrive as repeated fields; a comma-joined single value
        # (the older hidden-field form) is still accepted.
        categories_raw = [s for v in data.get('categories', []) for s in v.split(',')]
        regions_raw = [s for v in data.get('regions', []) for s in v.split(',')]

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
                # UnsubscribeAll=False: a signed double-opt-in from someone
                # who once hit "unsubscribe from all" is them asking back
                # in. Left set, the topic OPT_IN above would change nothing
                # and route_signup would keep sending them confirmations.
                ses.update_contact(
                    ContactListName=contact_list_name,
                    EmailAddress=email,
                    TopicPreferences=existing_preferences,
                    UnsubscribeAll=False,
                )

        # Re-validated here (not trusted from the confirm form as-is) the
        # same way route_confirm_link validated them going out — a confirm
        # POST is unsigned data riding alongside a signature that only ever
        # covered email/newsletters/timestamp.
        categories = _clean_slugs(
            categories_raw, set(db.get_all_categories().keys()))
        regions = _clean_slugs(regions_raw, set(REGIONS.keys()))
        if categories or regions:
            db.put_subscriber_preferences(email, categories, regions)

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
