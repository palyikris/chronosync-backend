from html.parser import HTMLParser
from pathlib import Path
import tomllib
from urllib.parse import parse_qs, urlsplit

import pytest

SUPABASE = Path(__file__).resolve().parents[1] / 'supabase'


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hrefs = []
    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self.hrefs.extend(value for key,value in attrs if key == 'href')


@pytest.mark.parametrize('name,path,kind',[
    ('invite','invite','invite'),('magic-link','magic','email'),('recovery','recover','recovery'),
])
def test_email_links_land_inertly_with_type_bound_token_and_manual_code(name,path,kind):
    source = (SUPABASE/'templates'/f'{name}.html').read_text()
    assert '{{ .Token }}' in source
    rendered = source.replace('{{ .SiteURL }}','https://app.example').replace('{{ .TokenHash }}','test-hash').replace('{{ .Token }}','12345678')
    links = Links()
    links.feed(rendered)
    assert len(links.hrefs) == 2
    action, manual = map(urlsplit,links.hrefs)
    assert action.path == manual.path == f'/auth/{path}'
    assert action.netloc == manual.netloc == 'app.example'
    assert parse_qs(action.query) == {'token_hash':['test-hash'],'type':[kind]}
    assert not manual.query
    assert '/auth/v1/verify' not in rendered and 'ConfirmationURL' not in source


def test_canonical_auth_configuration_is_invite_only_and_uses_exact_hosted_redirects():
    config = tomllib.loads((SUPABASE/'config.toml').read_text())['auth']
    assert config['enable_signup'] is False and config['email']['enable_signup'] is False
    assert config['enable_anonymous_sign_ins'] is False
    assert config['minimum_password_length'] >= 8
    assert config['email']['enable_confirmations'] is True
    assert config['email']['double_confirm_changes'] is True
    assert config['email']['otp_length'] == 8
    assert config['email']['otp_expiry'] == 3600
    assert config['enable_refresh_token_rotation'] is True
    assert config['site_url'] == 'https://chronosync-lovat.vercel.app'
    for url in config['additional_redirect_urls']:
        assert '*' not in url or url.startswith(('http://localhost:5173/','http://127.0.0.1:5173/'))
