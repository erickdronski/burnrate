"""Tests for secret redaction.

This module is the difference between a useful cost report and a credential
leak. `--format json` output gets pasted into issues and committed to repos, so
a raw `export API_KEY=sk-...` reaching it is the worst failure this package can
have — worse than being wrong about money.

Two halves, and both matter equally:

* Real credential shapes must be masked.
* Ordinary commands must survive untouched, because a redactor that mangles
  `git checkout <sha>` gets switched off, and then it protects nothing.
"""

import json
import unittest

from burnrate.redact import redact, redact_all
from burnrate.sessions import parse_file

from .test_sessions import TranscriptFixture, assistant, tool_use


def fake(prefix, body):
    """Build a credential-shaped test value without writing one in source.

    A literal `sk_live_...` in a committed file trips GitHub push protection
    and every secret scanner that later clones this repo — which is correct
    behavior on their part, and a real problem for a project whose test suite
    is *about* credential shapes. Composing the string at runtime keeps the
    tests honest (the value still matches the production patterns) without
    planting a scanner tripwire in the source.
    """
    return prefix + body


class TestMasksRealSecrets(unittest.TestCase):
    def assertMasked(self, text, secret):
        result = redact(text)
        self.assertNotIn(secret, result, "secret survived redaction: %r" % result)
        self.assertNotEqual(result, text)

    def test_anthropic_style_key(self):
        secret = fake("sk-", "ant-api03-" + "N0TAREALKEY" * 3)
        self.assertMasked("export ANTHROPIC_API_KEY=%s" % secret, secret)

    def test_github_token(self):
        secret = fake("ghp", "_" + "N0TAREALTOKEN" * 2)
        self.assertMasked("gh auth login --with-token %s" % secret, secret)

    def test_github_fine_grained_pat(self):
        secret = fake("github", "_pat_" + "N0TAREAL" * 4)
        self.assertMasked("export GH_TOKEN=%s" % secret, secret)

    def test_slack_token(self):
        secret = fake("xox", "b-000000000000-" + "N0TAREAL")
        self.assertMasked("curl -d token=%s" % secret, secret)

    def test_aws_access_key_id(self):
        secret = fake("AKIA", "N0TAREALKEYID000")
        self.assertMasked("aws configure set aws_access_key_id %s" % secret, secret)

    def test_google_api_key(self):
        secret = fake("AIza", "N0TAREAL" * 5)
        self.assertMasked("curl 'https://maps.googleapis.com/?key=%s'" % secret, secret)

    def test_stripe_live_key(self):
        secret = fake("sk", "_live_" + "N0TAREAL" * 3)
        self.assertMasked("stripe listen --api-key %s" % secret, secret)

    def test_gitlab_pat(self):
        secret = fake("glpat", "-" + "N0TAREAL" * 3)
        self.assertMasked("glab auth login --token %s" % secret, secret)

    def test_jwt(self):
        secret = fake("eyJ", "N0TAREALJWT." + "N0TAREALJWT." + "N0TAREALSIG0")
        self.assertMasked('curl -H "Authorization: Bearer %s" https://x' % secret, secret)

    def test_authorization_header(self):
        secret = fake("N0T", "AREALTOKEN0000000000")
        self.assertMasked('curl -H "Authorization: Token %s" https://x' % secret, secret)

    def test_password_assignment(self):
        secret = fake("N0T", "AREALPASSWORD")
        self.assertMasked("docker run -e DB_PASSWORD=%s img" % secret, secret)

    def test_password_flag(self):
        secret = fake("N0T", "AREALPASSWORD2")
        self.assertMasked("mysql --password %s" % secret, secret)

    def test_url_embedded_credential(self):
        secret = fake("N0T", "AREALDBPASS")
        self.assertMasked('psql "postgresql://user:%s@localhost/db"' % secret, secret)

    def test_private_key_block(self):
        secret = fake("MII", "N0TAREALPRIVATEKEY")
        text = "-----BEGIN RSA PRIVATE KEY-----\n%s\n-----END RSA PRIVATE KEY-----" % secret
        self.assertMasked(text, secret)

    def test_a_prefix_survives_so_keys_stay_distinguishable(self):
        result = redact("export API_KEY=" + fake("sk-", "ant-" + "N0TAREAL" * 3))
        self.assertIn("sk-", result)
        self.assertIn("redacted", result)


class TestLeavesOrdinaryCommandsAlone(unittest.TestCase):
    """A redactor that mangles normal output gets switched off."""

    def assertIntact(self, text):
        self.assertEqual(redact(text), text, "over-redacted: %r" % redact(text))

    def test_plain_commands(self):
        for command in (
            "npm run build && npm test",
            "python3 -m pytest tests/",
            "git status --porcelain",
            "ls -la ~/Projects",
            "docker compose up -d",
        ):
            with self.subTest(command=command):
                self.assertIntact(command)

    def test_commit_sha_is_not_a_secret(self):
        self.assertIntact("git checkout 3f2a9c8e1d4b7a6f5e0c9d8b7a6f5e4d3c2b1a09")

    def test_uuid_is_not_a_secret(self):
        self.assertIntact("open /tmp/f26ccfad-a644-4678-9f23-22a5deff75a3.json")

    def test_environment_reference_is_preserved(self):
        """Seeing that a command reads ${API_KEY} is the useful case."""
        self.assertIntact("export API_KEY=${ANTHROPIC_API_KEY}")
        self.assertIntact("curl -H \"Authorization: Bearer $TOKEN\" https://x")

    def test_word_password_in_a_test_filter(self):
        self.assertIntact("python3 -m pytest tests/test_auth.py -k password")

    def test_plain_urls_untouched(self):
        self.assertIntact("curl https://api.example.com/v1/things")
        self.assertIntact("open http://localhost:3000/admin")

    def test_ssh_and_git_remotes_untouched(self):
        self.assertIntact("ssh user@host.example.com")
        self.assertIntact("git remote add origin git@github.com:owner/repo.git")

    def test_placeholder_values_untouched(self):
        self.assertIntact("export API_KEY=changeme")

    def test_empty_and_none(self):
        self.assertEqual(redact(""), "")
        self.assertEqual(redact_all([]), [])


class TestRedactionHappensAtCapture(unittest.TestCase):
    """Redacting at capture means no output path can leak, including new ones."""

    def session_with_secret(self):
        secret = fake("sk-", "ant-api03-" + "N0TAREALVALUE" * 2)
        records = [
            assistant(
                "m",
                output_tokens=10,
                content=[tool_use("Bash", {"command": "export KEY=%s" % secret})],
            )
        ]
        return records, secret

    def test_secret_never_enters_the_session_object(self):
        records, secret = self.session_with_secret()
        with TranscriptFixture(records) as fixture:
            session = parse_file(fixture.path)
        self.assertTrue(session.commands)
        self.assertNotIn(secret, " ".join(session.commands))

    def test_secret_absent_from_json_output(self):
        from burnrate.cli import main
        import io
        from contextlib import redirect_stdout

        records, secret = self.session_with_secret()
        with TranscriptFixture(records) as fixture:
            out = io.StringIO()
            with redirect_stdout(out):
                main(["--root", fixture.root, "--format", "json"])
            payload = out.getvalue()
        self.assertNotIn(secret, payload)
        json.loads(payload)  # still valid JSON

    def test_secret_absent_from_verbose_text_output(self):
        from burnrate.cli import main
        import io
        from contextlib import redirect_stdout

        records, secret = self.session_with_secret()
        with TranscriptFixture(records) as fixture:
            out = io.StringIO()
            with redirect_stdout(out):
                main(["--root", fixture.root, "--verbose"])
            payload = out.getvalue()
        self.assertNotIn(secret, payload)


if __name__ == "__main__":
    unittest.main()
