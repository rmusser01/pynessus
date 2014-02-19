"""Library to talk with a remote Nessus 5 server that via its xmlrpc interface,

Methods mirror what is in the official API at
file:///C:/Users/Tmu/Desktop/nessus/nessus_5.0_XMLRPC_protocol_guide.pdf

Example usage:
  with Nessus('127.0.0.1:8443') as nes:
    nes.Login('admin', 'pass$%&(#'%#[]@:')
    logging.info('Feeds: %s', nes.Feed())

All calls can be done asynchronously:

  with Nessus('127.0.0.1:8443') as nes:
    def LoginCallback(result, error=None):
      if error:
        logging.warning('Error while logging: %s', error)
        return
      logging.info('Correcty logged in: %s', result)

    future = nes.Login('admin', 'pass$%&(#'%#[]@:', callback=LoginCallback)
    futures.wait([future])
    # At this point the LoginCallback is sure to have been called.
"""

from concurrent import futures
from urllib import request
import functools
import json
import logging
import os
import random
import urllib

HOST = 'https://127.0.0.1:8443'
_MAX_SEQ = 2 ** 31 - 1


class NessusError(Exception):
  pass


def FutureCallback(fn):
  @functools.wraps(fn)
  def wrapper(callback, future):
    if not future.done():
      raise NessusError('Future callback called before the future was finished.')
    try:
      contents = future.result()
    except (
        futures.CancelledError,
        futures.TimeoutError,
        Exception) as e:
      if callback:
        callback(None, error=NessusError(e))
        return
      else:
        raise NessusError(e)
    return fn(callback, contents)
  return wrapper

def SelfFutureCallback(fn):
  @functools.wraps(fn)
  def wrapper(self, callback, future):
    if not future.done():
      raise NessusError('Future callback called before the future was finished.')
    try:
      contents = future.result()
    except (
        futures.CancelledError,
        futures.TimeoutError,
        Exception) as e:
      raise NessusError(e)
    return fn(self, callback, contents)
  return wrapper


class Nessus(object):
  """Class to communicate with the remote nessus 5 instance.
  All methods support both synchronous and asynchronous calls.
  If synchronous calls are made, the returned values are the values from Nessus.
  If asynchronous cals are made, the returned value is a future that should be
  waited for using futures.wait([returned_future]) and the callback passed to
  the functions all have the same parameters:
  def Callback(result, error=None):
    pass
  If an error happened during the future execution, then result will be None
  and error will be an Exception (most likely a NessusError instance).
  """

  def __init__(self, host, executor=None, dump_path=None):
    """Initializes the Nessus instance.
    Does not connect on creation, this is a dumb constructor only,
    to connect call Login(...).

    Args:
      host: The host:port to connect to.
      executor: A concurrent.futures.Executor to use, if left unspecified a default
          ThreadPoolExecutor will be used with 5 parallel workers.
      dump_path: If specified all responses from nessus will be dumped to this
          path.
    """
    self._host = host
    self._session_token = None
    self._executor = executor or futures.ThreadPoolExecutor(max_workers=5)
    self._dump_path = dump_path

  def __enter__(self):
    return self

  def __exit__(self, type, value, traceback):
    """Exits a with: statement, automatically log out of nessus if connected."""
    if self._session_token:
      self.Logout()

  def _BuildRequest(self, path, data=None):
    request = urllib.request.Request(self._host + path + '?json=1')
    request.add_header('Content-Type', 'application/x-www-form-urlencoded;charset=utf-8')
    request.add_header('Accept', 'application/json')
    if data:
      data = urllib.parse.urlencode(data)
      data = data.encode('utf-8')
      request.data = data
    if self._session_token:
      # We are getting the header from nessus, so we should be fine against
      # header splitting attacks.
      request.add_header('Cookie', 'token=%s' % self._session_token)
    return request

  @staticmethod
  def _SendRequest(request, dump_path=None):
    logging.debug('Sending request to %s with data %s',
        request.get_full_url(), request.data)
    resp = urllib.request.urlopen(request)
    url_info = resp.info()
    encoding = url_info.get('Content-Encoding', 'utf-8')
    raw_json = resp.read().decode(encoding)
    logging.debug('urlopen returned \n%s\n', raw_json)
    if dump_path:
      with open(os.path.join(
          dump_path,
          request.selector[1:request.selector.find('?')].replace('/', '_') + '.json'),
          'w') as dump:
        dump.write(raw_json)
    json_resp = json.loads(raw_json)['reply']
    status = json_resp.get('status', '')
    if status != 'OK':
      raise NessusError('Status was not OK: %s: %s' % (status, json_resp['contents']))
    if 'contents' not in json_resp:
      return ''
    return json_resp['contents']

  @staticmethod
  def _SendRawRequest(request, dump_path=None):
    logging.debug('Sending request to %s with data %s',
        request.get_full_url(), request.data)
    resp = urllib.request.urlopen(request)
    url_info = resp.info()
    encoding = url_info.get('Content-Encoding', 'utf-8')
    decoded_raw = resp.read().decode(encoding)
    logging.debug('urlopen returned \n%s\n', decoded_raw)
    if dump_path:
      with open(os.path.join(
          dump_path,
          request.selector[1:request.selector.find('?')].replace('/', '_') + '.raw'),
          'w') as dump:
        dump.write(decoded_raw)
    return decoded_raw

  def Login(self, login, password, callback=None):
    data = {
      'login': login,
      'password': password,
      'seq': random.randint(1, _MAX_SEQ),
    }
    request = self._BuildRequest('/login', data)
    future = self._executor.submit(self._SendRequest, request, self._dump_path)
    if callback:
      future.add_done_callback(functools.partial(self._LoginDone, callback))
      return future
    else:
      futures.wait([future])
      return self._LoginDone(callback, future)

  @SelfFutureCallback
  def _LoginDone(self, callback, contents):
    self._session_token = contents['token']
    logging.debug('Token is %s', self._session_token)
    if callback:
      callback('Successully connected to Nessus')

  @staticmethod
  @FutureCallback
  def _SimpleReturnCB(callback, result):
    if callback:
      callback(result)
    else:
      return result

  def Logout(self, callback=None):
    data = {
      'seq': random.randint(1, _MAX_SEQ),
    }
    request = self._BuildRequest('/logout', data)
    future = self._executor.submit(self._SendRequest, request, self._dump_path)
    if callback:
      future.add_done_callback(functools.partial(self._LogoutDone, callback))
      return future
    else:
      futures.wait([future])
      return self._LogoutDone(callback, future)

  @SelfFutureCallback
  def _LogoutDone(self, callback, content):
    self._session_token = None
    if callback:
      callback('Logout from Nessus successful')
    
  @property
  def is_logged_in(self):
    return self._session_token is not None

  def Feed(self, callback=None):
    data = {
      'seq': random.randint(1, _MAX_SEQ),
    }
    request = self._BuildRequest('/feed', data)
    future = self._executor.submit(self._SendRequest, request, self._dump_path)
    if callback:
      future.add_done_callback(functools.partial(self._SimpleReturnCB, callback))
      return future
    else:
      futures.wait([future])
      return self._SimpleReturnCB(callback, future)

  def ListServerSettings(self, callback=None):
    data = {
      'seq': random.randint(1, _MAX_SEQ),
    }
    request = self._BuildRequest('/server/securesettings/list', data)
    future = self._executor.submit(self._SendRequest, request, self._dump_path)
    if callback:
      future.add_done_callback(
          functools.partial(self._ListServerSettingsDone, callback))
      return future
    else:
      futures.wait([future])
      return self._ListServerSettingsDone(callback, future)

  @staticmethod
  @FutureCallback
  def _ListServerSettingsDone(callback, contents):
    settings = contents.get('securesettings')
    if callback:
      callback(settings)
    else:
      return settings

  def PluginsDescriptions(self, callback=None):
    data = {
      'seq': random.randint(1, _MAX_SEQ),
    }
    request = self._BuildRequest('/plugins/descriptions', data)
    future = self._executor.submit(self._SendRequest, request, self._dump_path)
    if callback:
      future.add_done_callback(
          functools.partial(self._SimpleReturnCB, callback))
      return future
    else:
      futures.wait([future])
      return self._SimpleReturnCB(callback, future)
  
  def ListPreferences(self, callback=None):
    data = {
      'seq': random.randint(1, _MAX_SEQ),
    }
    request = self._BuildRequest('/server/preferences/list', data)
    future = self._executor.submit(self._SendRequest, request, self._dump_path)
    if callback:
      future.add_done_callback(
          functools.partial(self._ListPreferencesDone, callback))
      return future
    else:
      futures.wait([future])
      return self._ListPreferencesDone(callback, future)

  @staticmethod
  @FutureCallback
  def _ListPreferencesDone(callback, contents):
    return {
        pref['name']: pref['value'] for pref in contents[
            'serverpreferences']['preference']}

  def ServerLoad(self, callback=None):
    data = {
      'seq': random.randint(1, _MAX_SEQ),
    }
    request = self._BuildRequest('/server/load', data)
    future = self._executor.submit(self._SendRequest, request, self._dump_path)
    if callback:
      future.add_done_callback(
          functools.partial(self._ServerLoadDone, callback))
      return future
    else:
      futures.wait([future])
      return self._ServerLoadDone(callback, future)

  @staticmethod
  @FutureCallback
  def _ServerLoadDone(callback, contents):
    return contents['load'], contents['platform']

  def ServerUUID(self, callback=None):
    data = {
      'seq': random.randint(1, _MAX_SEQ),
    }
    request = self._BuildRequest('/uuid', data)
    future = self._executor.submit(self._SendRequest, request, self._dump_path)
    if callback:
      future.add_done_callback(
          functools.partial(self._ServerUUIDDone, callback))
      return future
    else:
      futures.wait([future])
      return self._ServerUUIDDone(callback, future)

  @staticmethod
  @FutureCallback
  def _ServerUUIDDone(callback, contents):
    return contents['uuid']

  def ServerCert(self, callback=None):
    data = {
      'seq': random.randint(1, _MAX_SEQ),
    }
    request = self._BuildRequest('/getcert', data)
    future = self._executor.submit(self._SendRawRequest, request, self._dump_path)
    if callback:
      future.add_done_callback(
          functools.partial(self._ServerCertDone, callback))
      return future
    else:
      futures.wait([future])
      return self._ServerCertDone(callback, future)

  @staticmethod
  @FutureCallback
  def _ServerCertDone(callback, contents):
    return contents

  def ListPlugins(self, callback=None):
    data = {
      'seq': random.randint(1, _MAX_SEQ),
    }
    request = self._BuildRequest('/plugins/list', data)
    future = self._executor.submit(self._SendRequest, request, self._dump_path)
    if callback:
      future.add_done_callback(
          functools.partial(self._ListPluginsDone, callback))
      return future
    else:
      futures.wait([future])
      return self._ListPluginsDone(callback, future)

  @staticmethod
  @FutureCallback
  def _ListPluginsDone(callback, contents):
    return {
        family['familyname']: int(family['numfamilymembers'])
        for family in contents['pluginfamilylist']['family']}

  def ListPluginsAttributes(self, callback=None):
    data = {
      'seq': random.randint(1, _MAX_SEQ),
    }
    request = self._BuildRequest('/plugins/attributes/list', data)
    future = self._executor.submit(self._SendRequest, request, self._dump_path)
    if callback:
      future.add_done_callback(
          functools.partial(self._ListPluginsAttributesDone, callback))
      return future
    else:
      futures.wait([future])
      return self._ListPluginsAttributesDone(callback, future)

  @staticmethod
  @FutureCallback
  def _ListPluginsAttributesDone(callback, contents):
    return contents['pluginsattributes']['attribute']

  def ListPluginsInFamily(self, family, callback=None):
    data = {
      'seq': random.randint(1, _MAX_SEQ),
      'family': family,
    }
    request = self._BuildRequest('/plugins/list/family', data)
    future = self._executor.submit(self._SendRequest, request, self._dump_path)
    if callback:
      future.add_done_callback(
          functools.partial(self._ListPluginsInFamilyDone, callback))
      return future
    else:
      futures.wait([future])
      return self._ListPluginsInFamilyDone(callback, future)

  @staticmethod
  @FutureCallback
  def _ListPluginsInFamilyDone(callback, contents):
    if contents['pluginlist']:
      return contents['pluginlist']['plugin']
    return []

  def AddUser(self, login, password, admin=False, callback=None):
    data = {
      'seq': random.randint(1, _MAX_SEQ),
      'login': login,
      'password': password,
      'admin': 1 if admin else 0,
    }
    request = self._BuildRequest('/users/add', data)
    future = self._executor.submit(self._SendRequest, request, self._dump_path)
    if callback:
      future.add_done_callback(
          functools.partial(self._AddUserDone, callback))
      return future
    else:
      futures.wait([future])
      return self._AddUserDone(callback, future)

  @staticmethod
  @FutureCallback
  def _AddUserDone(callback, contents):
    return contents['user']

  def ListPolicies(self, callback=None):
    data = {
      'seq': random.randint(1, _MAX_SEQ),
    }
    request = self._BuildRequest('/policy/list', data)
    future = self._executor.submit(self._SendRequest, request, self._dump_path)
    if callback:
      future.add_done_callback(
          functools.partial(self._ListPoliciesDone, callback))
      return future
    else:
      futures.wait([future])
      return self._ListPoliciesDone(callback, future)

  @staticmethod
  @FutureCallback
  def _ListPoliciesDone(callback, contents):
    return contents['policies']

  def NewScan(self, targets, policy_id, scan_name, callback=None):
    data = {
      'seq': random.randint(1, _MAX_SEQ),
      'target': ','.join(targets),
      'policy_id': policy_id,
      'scan_name': scan_name,
    }
    request = self._BuildRequest('/scan/new', data)
    future = self._executor.submit(self._SendRequest, request, self._dump_path)
    if callback:
      future.add_done_callback(
          functools.partial(self._NewScanDone, callback))
      return future
    else:
      futures.wait([future])
      return self._NewScanDone(callback, future)

  @staticmethod
  @FutureCallback
  def _NewScanDone(callback, contents):
    return contents['scan']

  def ListReports(self, callback=None):
    data = {
      'seq': random.randint(1, _MAX_SEQ),
    }
    request = self._BuildRequest('/report/list', data)
    future = self._executor.submit(self._SendRequest, request, self._dump_path)
    if callback:
      future.add_done_callback(
          functools.partial(self._ListReportsDone, callback))
      return future
    else:
      futures.wait([future])
      return self._ListReportsDone(callback, future)

  @staticmethod
  @FutureCallback
  def _ListReportsDone(callback, contents):
    return contents['reports']

  def GetReport(self, uuid, callback=None):
    data = {
      'seq': random.randint(1, _MAX_SEQ),
      'report': uuid,
    }
    request = self._BuildRequest('/file/report/download', data)
    future = self._executor.submit(self._SendRawRequest, request, self._dump_path)
    if callback:
      future.add_done_callback(
          functools.partial(self._GetReportDone, callback))
      return future
    else:
      futures.wait([future])
      return self._GetReportDone(callback, future)

  @staticmethod
  @FutureCallback
  def _GetReportDone(callback, contents):
    return contents


if __name__ == '__main__':
  logging.basicConfig(
      level=logging.DEBUG,
      format='[%(levelname)s] (%(threadName)-10s) %(message)s',
  )

  def callback(status):
    logging.info('Future finished: %s', status)
  with Nessus(HOST, dump_path='C:\\Users\\Tmu\\Desktop\\tmp') as nessus:
    nessus.Login('admin', 'simplerpass')
    #logging.info('Feed: %s', nessus.Feed())
    #logging.info('Server settings: %s', nessus.ListServerSettings())
    #plugins = nessus.PluginsDescriptions()
    #logging.info(nessus.ListPreferences())
    #logging.info(nessus.ServerLoad())
    #logging.info(nessus.ServerUUID())
    #logging.info(nessus.ServerCert())
    #logging.info(nessus.ListPlugins())
    #logging.info(nessus.ListPluginsAttributes())
    #logging.info(nessus.ListPluginsInFamily('General'))
    #logging.info(nessus.AddUser('adm12', 'testpass', False))
    #logging.info(nessus.ListPolicies())
    #logging.info(nessus.NewScan(['192.168.0.1'], '-1', 'localhost scan'))
    #logging.info(nessus.ListReports())
    #logging.info(nessus.GetReport("f4f71476-9297-49be-76ed-bac6c4d890ee110ef9d2c656c3a0"))
