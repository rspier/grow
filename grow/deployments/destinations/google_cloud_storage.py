from . import base
from . import messages as deployment_messages
from boto.gs import key
from protorpc import messages
import boto
import os
import cStringIO
import dns.resolver
import logging
import mimetypes
import webapp2


class Config(messages.Message):
  project = messages.StringField(1)
  bucket = messages.StringField(2)
  access_key = messages.StringField(3)
  access_secret = messages.StringField(4)


class TestCase(base.DeploymentTestCase):

  def test_domain_cname_is_gcs(self):
    bucket_name = self.deployment.config.bucket
    CNAME = 'c.storage.googleapis.com'

    message = deployment_messages.TestResultMessage()
    message.title = 'CNAME for {} is {}'.format(bucket_name, CNAME)

    dns_resolver = dns.resolver.Resolver()
    dns_resolver.nameservers = ['8.8.8.8']  # Use Google's DNS.

    try:
      content = str(dns_resolver.query(bucket_name, 'CNAME')[0])
    except:
      text = "Can't verify CNAME for {} is mapped to {}"
      message.result = deployment_messages.Result.WARNING
      message.text = text.format(bucket_name, CNAME)

    if not content.startswith(CNAME):
      text = 'CNAME mapping for {} is not GCS! Found {}, expected {}'
      message.result = deployment_messages.Result.WARNING
      message.text = text.format(bucket_name, content, CNAME)
    else:
      text = 'CNAME for {} -> {}'.format(bucket_name, content, CNAME)
      message.text = text.format(text, content, CNAME)

    return message


class GoogleCloudStorageDeployment(base.BaseDeployment):
  NAME = 'gcs'
  TestCase = TestCase
  Config = Config

  def __str__(self):
    return 'gs://{}'.format(self.config.bucket)

  def write_control_file(self, path, content):
    path = os.path.join(self.control_dir, path.lstrip('/'))
    return self.write_file(path, content, policy='private')

  def read_file(self, path):
    file_key = key.Key(self.bucket)
    file_key.key = path
    try:
      return file_key.get_contents_as_string()
    except boto.exception.GSResponseError, e:
      if e.status != 404:
        raise
      raise IOError('File not found: {}'.format(path))

  def delete_file(self, path):
    bucket_key = key.Key(self.bucket)
    bucket_key.key = path.lstrip('/')
    self.bucket.delete_key(bucket_key)

  @webapp2.cached_property
  def bucket(self):
    connection = boto.connect_gs(self.config.access_key,
                                 self.config.access_secret,
                                 is_secure=False)
    return connection.get_bucket(self.config.bucket)

  def prelaunch(self, dry_run=False):
    if dry_run:
      return
    logging.info('Configuring GCS bucket: {}'.format(self.config.bucket))
    self.bucket.set_acl('public-read')
    self.bucket.configure_versioning(False)
    self.bucket.configure_website(main_page_suffix='index.html', error_key='404.html')

  def write_file(self, path, content, policy='public-read'):
    path = path.lstrip('/')
    if isinstance(content, unicode):
      content = content.encode('utf-8')
    bucket_key = key.Key(self.bucket)
    bucket_key.key = path
    fp = cStringIO.StringIO()
    fp.write(content)
    # TODO(jeremydw): Better headers.
    headers = {
        'Cache-Control': 'no-cache',
        'Content-Type': mimetypes.guess_type(path)[0],
    }
    fp.seek(0)
    bucket_key.set_contents_from_file(fp, headers=headers, replace=True, policy=policy)
    fp.close()
