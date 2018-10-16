"""
Everything in o4 revolves around fstat lines. In this file are the
methods that manage local cache, communicate with fstat server and
perforce.

Starting from the top. This is what fstat lines look like:

# COLUMNS: F_CHANGELIST, F_PATH, F_REVISION, F_FILE_SIZE, F_CHECKSUM
16713356,sfdc-test/build/buildtest.txt,447,84,95966F39451957FB9FBCFD8E3CAF3826
16713340,ui-action-components/pom.xml,1,5955,E60791275270B2F0F9F931635A4F1D0F
16713340,ui-parent/pom.xml,301,66281,3FE6DF9C88349817ACD82A0B0A49053B
16712952,ui-communities-components/components/forceTopic/featuredTopicItem/featuredTopicItem.css,3,0,,
16643751,sfdc-test/func/results/Charts/testCatalan (Spain;.Euro).pcscript,4,2456,1AC64FF827B21C29B0CD5A2F1EC8A31C
16267144,sfdc/htdocs/apple-app-site-association-default,1,39/symlink,2B02BAB34519B851BB66B1C1735C59EB
16267144,sfdc/htdocs/apple-app-site-association,5,0/symlink,

Each line has all the information needed for syncing and verifying a
single file:

* Most recent CHANGELIST
* The relative PATH, commas are escaped as ';.' and semicolons as ';;'
* The associated REVISION at that changelist
* The file SIZE in bytes, potentially combined with utf8, utf16 or symlink
* The CHECKSUM for the content

Fstat lines are always relative to a depot, so you can not mix fstat
output from two different depots into one pipeline.

Local cache is managed in the `.o4` directory. This is the reason o4
can not sync single files. It needs to store its cache and state in
.o4 inside the parent directory of the depot sync. Since a file is,
well, a file, there can not be a directory in it.

The .o4 directory may contain the following files:

* head - a guidance to o4 telling what the depot's current head can be
  assumed to be

* changelist - the most recent changelist synced to with o4. This is
  the starting point for all syncs. If the user has synced without o4
  in between, o4 will verify that everything that is different since
  it last run is now in place. o4 does not care about the have list.

* 123.fstat.gz - a gzipped local cache of the depot's most recent
  change for every single file up to and including the changelist in
  the file name (in this case 123).
"""

import os
import sys
import gzip

from o4_progress import progress_iter
import o4_config

F_CHANGELIST, F_PATH, F_REVISION, F_FILE_SIZE, F_CHECKSUM = range(5)


class FstatRedirection(Exception):

    def __init__(self, cl):
        self.cl = cl


class FstatMalformed(Exception):
    """Raised when an fstat line is malformed."""


def fstat_join(f):
    """
    Combines fstat columns into a string, properly escaped.
    """
    try:
        p = f[1].replace(';', ';;').replace(',', ';.')
        # Make sure we don't unnecessarily cause a tuple to list conversion:
        if len(p) != len(f[1]):
            f[1] = p
        return ','.join(f)
    except TypeError:
        f = [str(i) for i in f]
        f[1] = p
    return ','.join(f)


def fstat_split(line):
    """
    Splits an fstat line into its 5 (five) constituent parts:
    F_CHANGELIST, F_REVISION, F_FILE_SIZE, _, _, F_CHECKSUM, F_PATH

    If the line has fewer than 4 (four) commas, the line is assumed
    malformed unless it starts with '#' or is empty. For
    non-comforming but not malformed lines, None is returned.
    """
    line = line.rstrip()
    if not line or line[0] == '#':
        return None
    res = line.strip().split(',', 6)
    if len(res) == 5:
        res[F_PATH] = res[F_PATH].replace(';.', ',').replace(';;', ';')
        return res
    if len(res) != 7:
        raise FstatMalformed(line)
    # Old format:
    # F_CHANGELIST, F_REVISION, F_FILE_SIZE, 3:F_LAST_ACTION, 4:F_FILE_TYPE, F_CHECKSUM, F_PATH
    if res[4].startswith('utf') or res[4] == 'symlink':
        res[2] = res[2] + '/' + res[4]
    return res[0], res[6], res[1], res[2], res[5]


def fstat_cl_path(line):
    """
    Extracts integer changelist and path from an fstat line. If the
    line has fewer than 4 (four) commas, the line is assumed malformed
    unless it starts with '#' or is empty, in which case the returned
    changelist and path is None.

    Returns a tuple: (changelist, path, line)
    """
    line = line.rstrip()
    if not line or line[0] == '#':
        return None, None, line
    cl, path, _ = line.split(',', 2)
    if path.isdigit():
        cl, path = fstat_split(line)[:2]
    else:
        path = path.replace(';.', ',').replace(';;', ';')
    return int(cl), path, line


def fstat_path(line):
    """
    Extracts path from an fstat line, unless it starts with '#' or is
    empty, in which case the returned path is None.

    Returns a tuple: (path, line)
    """
    line = line.rstrip()
    if not line or line[0] == '#':
        return None, line
    _, path, _ = line.split(',', 2)
    if path.isdigit():
        _, path = fstat_split(line)[:2]
    else:
        path = path.replace(';.', ',').replace(';;', ';')
    return path, line


def fstat_cl(line):
    """
    Extracts integer changelist from an fstat line, unless it starts
    with '#' or is empty, in which case the returned changelist is 0.

    Returns a tuple: changelist, line
    """
    line = line.rstrip()
    if not line or line[0] == '#':
        return 0, line
    cl, _ = line.split(',', 1)
    return int(cl), line


def fstat_from_csv(fname, split=None):
    """
    Returns a mapped iterator over lines in fname. If split is None,
    each line has its newline removed.
    """
    if split is None:
        split = lambda x: x[:-1]
    with gzip.open(fname, 'rt', encoding='utf8') as fin:
        for line in fin:
            if split is None:
                yield line.strip()
            else:
                yield split(line)


def get_fstat_cache(changelist, o4_dir='.o4'):
    """
    Returns a tuple of (changelist, path) to the most recent fstat
    cache file less than changelist. If there isn't one, (None, None)
    is returned.
    """
    import glob
    changelist = int(changelist)
    fstats = glob.glob(f'{o4_dir}/*.fstat.gz')
    cls = sorted(
        (int(os.path.basename(f).split('.', 1)[0]) for f in fstats),
        key=lambda x: (abs(x - changelist), x))
    cls = [c for c in cls if c <= changelist]
    if cls:
        return cls[0], f"{o4_dir}/{cls[0]}.fstat.gz"
    return 0, None


def fstat_iter(depot_path, to_changelist, from_changelist=0, cache_dir='.o4'):
    '''
    Return the needed fstat data by combining three possible sources: perforce,
    the fstat server, and local fstat cache files.
    Note that the local files and the fstat server are guaranteed to return lines
    in (descending) changelist order, while the Perforce data may not be.
    The three sources are ordered [fstat server, perforce, fstat server, local];
    each one may or may not be used, and the fstat server will not be used twice.
    In the order read, each subset will contain only changelist numbers less than
    all that have been read in previous subsets.
    The local cache file created should not have more than one entry for any
    filename. Such duplication may come about due to a file having been changed in
    more than one of the changelist subsets being queried; a row for a file that
    has been seen already (and thus, at a higher changelist) must be ignored.

    Beware: do not break out of the returned generator! This will
    prevent local cache files from being created, causing superfluous
    access to perforce and/or fstat server.
    '''
    from tempfile import mkstemp
    from o4_pyforce import P4TimeoutError, P4Error

    to_changelist, from_changelist = int(to_changelist), int(from_changelist)
    cache_cl, cache_fname = get_fstat_cache(to_changelist, cache_dir)
    updated = []
    all_filenames = set()
    CLR = '%c[2K\r' % chr(27)

    summary = {'Perforce': None, 'Fstat server': None, 'Local cache': None}

    try:
        fout = temp_fname = None
        highest_written_cl = 0
        _first = _last = 0  # These are local and re-used in various blocks below
        fh, temp_fname = mkstemp(dir=cache_dir)
        os.close(fh)
        fout = gzip.open(temp_fname, 'wt', encoding='utf8', compresslevel=9)
        print("# COLUMNS: F_CHANGELIST, F_PATH, F_REVISION, F_FILE_SIZE, F_CHECKSUM", file=fout)

        if cache_cl == to_changelist:
            print(f'*** INFO: Satisfied from local cache {cache_fname}', file=sys.stderr)
            for cl, line in fstat_from_csv(cache_fname, fstat_cl):
                if not cl:
                    continue
                if cl < from_changelist:
                    break
                yield line
            return

        missing_range = (to_changelist, cache_cl + 1)
        o4server_range = (None, None)

        if o4_config.fstat_server():
            _first = _last = 0
            try:
                for line in fstat_from_server(depot_path, missing_range[0], missing_range[1],
                                              o4_config.fstat_server_nearby()):
                    f = fstat_split(line)
                    if not f:
                        continue
                    _last = f[F_CHANGELIST]
                    _first = _first or f[F_CHANGELIST]
                    all_filenames.add(f[F_PATH])
                    print(line, file=fout)
                    yield line
                summary['Fstat server'] = (o4server_range, (int(_first), int(_last)))
                missing_range = (None, None)
            except FstatRedirection as e:
                print(f'*** INFO: Fstat server redirected to changelist {e.cl}', file=sys.stderr)
                if e.cl > to_changelist:
                    print(f'*** WARNING: Fstat server redirected to {e.cl} which is greater',
                          f'than {to_changelist}.')
                    print('             Please contact workspaceengineering@salesforce.com.')
                elif e.cl > cache_cl:
                    missing_range = (to_changelist, e.cl + 1)
                    o4server_range = (e.cl, cache_cl + 1)
        highest_written_cl = max(highest_written_cl, int(_first))

        perforce_filenames = dict()
        if missing_range[0]:
            retry = 3
            while retry:
                retry -= 1
                try:
                    for f in fstat_from_perforce(depot_path, missing_range[0], missing_range[1]):
                        if f[F_PATH] and f[F_PATH] not in all_filenames:
                            if from_changelist < int(f[F_CHANGELIST]) <= to_changelist:
                                yield fstat_join(f)
                            f[0] = int(f[0])
                            perforce_filenames[f[F_PATH]] = f
                    break
                except P4Error as e:
                    fix = False
                    for a in e.args:
                        if 'Too many rows scanned' in a.get('data', ''):
                            if cache_cl:
                                print(
                                    f"{CLR}*** WARNING: Maxrowscan occurred, ignoring cache {cache_fname}@{cache_cl}",
                                    file=sys.stderr)
                                fix = True
                                cache_cl = cache_fname = None
                                retry += 1
                        elif 'Request too large' in a.get('data', ''):
                            sys.exit(
                                f"{CLR}*** ERROR: 'Request too large'. {depot_path} may be too broad."
                            )
                    if not fix:
                        raise
                except P4TimeoutError:
                    perforce_filenames.clear()
                    print(f"{CLR}*** WARNING: ({retry+1}/3) P4 Timeout while getting fstat")
            else:
                sys.exit(f"{CLR}*** ERROR: "
                         f"Too many P4 Timeouts for p4 fstat"
                         f"{depot_path}@{from_changelist},@{to_changelist}")

        all_filenames.update(perforce_filenames.keys())
        if perforce_filenames:
            perforce_rows = sorted(perforce_filenames.values(), reverse=True)
            summary['Perforce'] = (missing_range, (int(perforce_rows[0][F_CHANGELIST]),
                                                   int(perforce_rows[-1][F_CHANGELIST])))
            highest_written_cl = max(highest_written_cl, int(perforce_rows[0][F_CHANGELIST]))
            for f in perforce_rows:
                print(*f, sep=',', file=fout)
            del perforce_filenames

        if o4server_range[0]:
            _first = _last = 0
            for line in fstat_from_server(depot_path, o4server_range[0], o4server_range[1]):
                f = fstat_split(line)
                if not f:
                    continue
                _last = f[F_CHANGELIST]
                _first = _first or f[F_CHANGELIST]
                if f[F_PATH] not in all_filenames:
                    all_filenames.add(f[F_PATH])
                    print(line, file=fout)
                    if (from_changelist < int(f[F_CHANGELIST]) <= to_changelist):
                        yield line
            summary['Fstat server'] = (o4server_range, (int(_first), int(_last)))
            highest_written_cl = max(highest_written_cl, int(_first))

        if cache_cl:
            convert = False
            _first = _last = 0
            for cl, filename, line in fstat_from_csv(cache_fname, fstat_cl_path):
                if not filename:
                    if line.startswith('# COLUMNS:'):
                        if 'F_LAST_ACTION' in line or 'F_FILE_TYPE' in line:
                            convert = True
                    continue
                if all_filenames and filename in all_filenames:
                    all_filenames.remove(filename)
                    continue
                _first = _first or cl
                if convert:
                    line = fstat_join(fstat_split(line))
                if from_changelist < cl <= to_changelist:
                    _last = cl
                    yield line
                print(line, file=fout)
            summary['Local cache'] = ((cache_cl, 1), (int(_first), int(_last)))
            highest_written_cl = max(highest_written_cl, int(_first))

        fout.close()
        fout = None
        if highest_written_cl:
            os.chmod(temp_fname, 0o444)
            os.rename(temp_fname, f'{cache_dir}/{highest_written_cl}.fstat.gz')
    finally:
        if fout:
            fout.close()
        try:
            if temp_fname:
                os.unlink(temp_fname)
        except FileNotFoundError:
            pass

    from texttable import Texttable
    table = Texttable()
    table.set_cols_align(['l', 'l', 'l'])
    table.set_header_align(['l', 'l', 'l'])
    table.header(['Fstat source', 'Requested', 'Provided'])
    table.set_chars(['-', '|', '+', '-'])
    table.set_deco(table.HEADER)
    for k in 'Perforce', 'Fstat server', 'Local cache':
        data = summary[k] if summary[k] else ('Not used', '')
        if summary[k]:
            v = summary[k]
            data = ('{:10,} - {:10,}'.format((v[0][0] or 0), (v[0][1] or 0)),
                    '{:10,} - {:10,}'.format((v[1][0] or 0), (v[1][1] or 0)))
        else:
            data = ('Not used', '')
        table.add_row([k, data[0], data[1]])
    table = '\n'.join('*** INFO: ' + row for row in table.draw().split('\n'))
    print(table, file=sys.stderr)


def fstat_from_perforce(depot_path, upper, lower=None):
    """
    Returns an iterator of Fstat objects where changelist is in
    (lower, upper]. If lower is not given, it is assumed to be 0.
    """

    from o4_pyforce import Pyforce

    def fstatify(r, head=len(depot_path.replace('...', ''))):
        try:
            if r[b'code'] == b'skip':
                return ('0', '', '0', '0', '')
            t = r[b'headType'].decode('utf8')
            sz = r.get(b'fileSize', b'0').decode('utf8')
            if t.startswith('utf') or t == 'symlink':
                sz = sz + '/' + t
            else:
                c = r.get(b'digest', b'').decode('utf8')
            return [
                r[b'headChange'].decode('utf8'),
                Pyforce.unescape(r[b'depotFile'].decode('utf8'))[head:].replace(';', ';;').replace(
                    ',', ';.'), r[b'headRev'].decode('utf8'), sz, c
            ]
        except StopIteration:
            raise
        except Exception as e:
            print("*** ERROR: Got {!r} while fstatify({!r})".format(e, r), file=sys.stderr)
            raise

    revs = '@{}'.format(upper)
    if lower > 1:
        # A range going back to the beginning will get a Perforce error.
        assert lower <= upper
        revs = '@{},@{}'.format(lower, upper)
    pyf = Pyforce('fstat', '-Rc', '-Ol', '-Os', '-T',
                  'headType, digest, fileSize, depotFile, headChange, headRev',
                  Pyforce.escape(depot_path) + revs)
    pyf.transform = fstatify
    return pyf


def fstat_from_server(depot_path, upper, lower, nearby=None):
    import requests

    if not o4_config.fstat_server():
        raise Exception('fstat_server is not configured')

    class filebridge(object):
        'Converts read() requests to iteration requests on a requests stream.'

        def __init__(self, stream):
            self.stream = stream.iter_content(chunk_size=1024 * 1024)
            self.buffer = b''

        def read(self, n):
            if n > len(self.buffer):
                try:
                    self.buffer += next(self.stream)
                except StopIteration:
                    pass
            ret = self.buffer[:n]
            self.buffer = self.buffer[n:]
            return ret

    depot_path = depot_path.replace('//', '').replace('/...', '')
    url = f'{o4_config.fstat_server()}/o4-http/fstat/{upper}/{depot_path}'
    if nearby:
        url += f'?nearby={nearby}'
    print(f'*** INFO: Fetching {url}', file=sys.stderr)
    server = requests.get(url, stream=True, allow_redirects=False)
    if server.status_code == 404:
        raise Exception(f'Unknown fstat request:  {url}')
    if server.status_code // 100 == 3:
        redir = server.headers['Location']
        us = url.split('/')
        rs = redir.split('/')
        cl = int([i for i in zip(us, rs) if i[0] != i[1]][0][1])
        raise FstatRedirection(cl)

    if server.status_code != 200:
        raise Exception(f'*** WARNING: Status {server.status_code} from {url}')
    g = gzip.GzipFile(fileobj=filebridge(server))
    while True:
        line = g.readline().decode('utf-8')
        yield line


##
# Copyright (c) 2018, salesforce.com, inc.
# All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# For full license text, see the LICENSE file in the repo root or https://opensource.org/licenses/BSD-3-Clause
