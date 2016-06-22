#!/usr/bin/env python
#
# Copyright (c) 2012 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Process Android resources to generate R.java, and prepare for packaging.

This will crunch images and generate v14 compatible resources
(see generate_v14_compatible_resources.py).
"""

import codecs
import collections
import optparse
import os
import re
import shutil
import sys
import xml.etree.ElementTree

import generate_v14_compatible_resources

from util import build_utils

# Import jinja2 from third_party/jinja2
sys.path.insert(1,
    os.path.join(os.path.dirname(__file__), '../../../third_party'))
from jinja2 import Template # pylint: disable=F0401


# Represents a line from a R.txt file.
TextSymbolsEntry = collections.namedtuple('RTextEntry',
    ('java_type', 'resource_type', 'name', 'value'))


def _ParseArgs(args):
  """Parses command line options.

  Returns:
    An options object as from optparse.OptionsParser.parse_args()
  """
  parser = optparse.OptionParser()
  build_utils.AddDepfileOption(parser)

  parser.add_option('--android-sdk-jar',
                    help='the path to android jar file.')
  parser.add_option('--aapt-path',
                    help='path to the Android aapt tool')
  parser.add_option('--non-constant-id', action='store_true')

  parser.add_option('--android-manifest', help='AndroidManifest.xml path')
  parser.add_option('--custom-package', help='Java package for R.java')
  parser.add_option(
      '--shared-resources',
      action='store_true',
      help='Make a resource package that can be loaded by a different'
      'application at runtime to access the package\'s resources.')
  parser.add_option(
      '--app-as-shared-lib',
      action='store_true',
      help='Make a resource package that can be loaded as shared library.')

  parser.add_option('--resource-dirs',
                    help='Directories containing resources of this target.')
  parser.add_option('--dependencies-res-zips',
                    help='Resources from dependents.')

  parser.add_option('--resource-zip-out',
                    help='Path for output zipped resources.')

  parser.add_option('--R-dir',
                    help='directory to hold generated R.java.')
  parser.add_option('--srcjar-out',
                    help='Path to srcjar to contain generated R.java.')
  parser.add_option('--r-text-out',
                    help='Path to store the R.txt file generated by appt.')

  parser.add_option('--proguard-file',
                    help='Path to proguard.txt generated file')

  parser.add_option(
      '--v14-skip',
      action="store_true",
      help='Do not generate nor verify v14 resources')

  parser.add_option(
      '--extra-res-packages',
      help='Additional package names to generate R.java files for')
  parser.add_option(
      '--extra-r-text-files',
      help='For each additional package, the R.txt file should contain a '
      'list of resources to be included in the R.java file in the format '
      'generated by aapt')
  parser.add_option(
      '--include-all-resources',
      action='store_true',
      help='Include every resource ID in every generated R.java file '
      '(ignoring R.txt).')

  parser.add_option(
      '--all-resources-zip-out',
      help='Path for output of all resources. This includes resources in '
      'dependencies.')

  parser.add_option('--stamp', help='File to touch on success')

  options, positional_args = parser.parse_args(args)

  if positional_args:
    parser.error('No positional arguments should be given.')

  # Check that required options have been provided.
  required_options = (
      'android_sdk_jar',
      'aapt_path',
      'android_manifest',
      'dependencies_res_zips',
      'resource_dirs',
      'resource_zip_out',
      )
  build_utils.CheckOptions(options, parser, required=required_options)

  if (options.R_dir is None) == (options.srcjar_out is None):
    raise Exception('Exactly one of --R-dir or --srcjar-out must be specified.')

  options.resource_dirs = build_utils.ParseGypList(options.resource_dirs)
  options.dependencies_res_zips = (
      build_utils.ParseGypList(options.dependencies_res_zips))

  # Don't use [] as default value since some script explicitly pass "".
  if options.extra_res_packages:
    options.extra_res_packages = (
        build_utils.ParseGypList(options.extra_res_packages))
  else:
    options.extra_res_packages = []

  if options.extra_r_text_files:
    options.extra_r_text_files = (
        build_utils.ParseGypList(options.extra_r_text_files))
  else:
    options.extra_r_text_files = []

  return options


def CreateRJavaFiles(srcjar_dir, main_r_txt_file, packages, r_txt_files,
                     shared_resources):
  assert len(packages) == len(r_txt_files), 'Need one R.txt file per package'

  # Map of (resource_type, name) -> Entry.
  # Contains the correct values for resources.
  all_resources = {}
  for entry in _ParseTextSymbolsFile(main_r_txt_file):
    all_resources[(entry.resource_type, entry.name)] = entry

  # Map of package_name->resource_type->entry
  resources_by_package = (
      collections.defaultdict(lambda: collections.defaultdict(list)))
  # Build the R.java files using each package's R.txt file, but replacing
  # each entry's placeholder value with correct values from all_resources.
  for package, r_txt_file in zip(packages, r_txt_files):
    if package in resources_by_package:
      raise Exception(('Package name "%s" appeared twice. All '
                       'android_resources() targets must use unique package '
                       'names, or no package name at all.') % package)
    resources_by_type = resources_by_package[package]
    # The sub-R.txt files have the wrong values at this point. Read them to
    # figure out which entries belong to them, but use the values from the
    # main R.txt file.
    for entry in _ParseTextSymbolsFile(r_txt_file):
      entry = all_resources[(entry.resource_type, entry.name)]
      resources_by_type[entry.resource_type].append(entry)

  for package, resources_by_type in resources_by_package.iteritems():
    package_r_java_dir = os.path.join(srcjar_dir, *package.split('.'))
    build_utils.MakeDirectory(package_r_java_dir)
    package_r_java_path = os.path.join(package_r_java_dir, 'R.java')
    java_file_contents = _CreateExtraRJavaFile(
        package, resources_by_type, shared_resources)
    with open(package_r_java_path, 'w') as f:
      f.write(java_file_contents)


def _ParseTextSymbolsFile(path):
  """Given an R.txt file, returns a list of TextSymbolsEntry."""
  ret = []
  with open(path) as f:
    for line in f:
      m = re.match(r'(int(?:\[\])?) (\w+) (\w+) (.+)$', line)
      if not m:
        raise Exception('Unexpected line in R.txt: %s' % line)
      java_type, resource_type, name, value = m.groups()
      ret.append(TextSymbolsEntry(java_type, resource_type, name, value))
  return ret


def _CreateExtraRJavaFile(package, resources_by_type, shared_resources):
  """Generates the contents of a R.java file."""
  # Keep these assignments all on one line to make diffing against regular
  # aapt-generated files easier.
  create_id = ('{{ e.resource_type }}.{{ e.name }} = '
               '({{ e.resource_type }}.{{ e.name }} & 0x00ffffff) |'
               ' (packageId << 24);')
  create_id_arr = ('{{ e.resource_type }}.{{ e.name }}[i] = '
                   '({{ e.resource_type }}.{{ e.name }}[i] & 0x00ffffff) |'
                   ' (packageId << 24);')
  template = Template("""/* AUTO-GENERATED FILE.  DO NOT MODIFY. */

package {{ package }};

public final class R {
    {% for resource_type in resource_types %}
    public static final class {{ resource_type }} {
        {% for e in resources[resource_type] %}
        {% if shared_resources %}
        public static {{ e.java_type }} {{ e.name }} = {{ e.value }};
        {% else %}
        public static final {{ e.java_type }} {{ e.name }} = {{ e.value }};
        {% endif %}
        {% endfor %}
    }
    {% endfor %}
    {% if shared_resources %}
    public static void onResourcesLoaded(int packageId) {
        {% for resource_type in resource_types %}
        {% for e in resources[resource_type] %}
        {% if resource_type != 'styleable' and e.java_type != 'int[]' %}
        """ + create_id + """
        {% endif %}
        {% endfor %}
        {% for e in resources[resource_type] %}
        {% if e.java_type == 'int[]' %}
        for(int i = 0; i < {{ e.resource_type }}.{{ e.name }}.length; ++i) {
            """ + create_id_arr + """
        }
        {% endif %}
        {% endfor %}
        {% endfor %}
    }
    {% endif %}
}
""", trim_blocks=True, lstrip_blocks=True)

  return template.render(package=package,
                         resources=resources_by_type,
                         resource_types=sorted(resources_by_type),
                         shared_resources=shared_resources)


def CrunchDirectory(aapt, input_dir, output_dir):
  """Crunches the images in input_dir and its subdirectories into output_dir.

  If an image is already optimized, crunching often increases image size. In
  this case, the crunched image is overwritten with the original image.
  """
  aapt_cmd = [aapt,
              'crunch',
              '-C', output_dir,
              '-S', input_dir,
              '--ignore-assets', build_utils.AAPT_IGNORE_PATTERN]
  build_utils.CheckOutput(aapt_cmd, stderr_filter=FilterCrunchStderr,
                          fail_func=DidCrunchFail)

  # Check for images whose size increased during crunching and replace them
  # with their originals (except for 9-patches, which must be crunched).
  for dir_, _, files in os.walk(output_dir):
    for crunched in files:
      if crunched.endswith('.9.png'):
        continue
      if not crunched.endswith('.png'):
        raise Exception('Unexpected file in crunched dir: ' + crunched)
      crunched = os.path.join(dir_, crunched)
      original = os.path.join(input_dir, os.path.relpath(crunched, output_dir))
      original_size = os.path.getsize(original)
      crunched_size = os.path.getsize(crunched)
      if original_size < crunched_size:
        shutil.copyfile(original, crunched)


def FilterCrunchStderr(stderr):
  """Filters out lines from aapt crunch's stderr that can safely be ignored."""
  filtered_lines = []
  for line in stderr.splitlines(True):
    # Ignore this libpng warning, which is a known non-error condition.
    # http://crbug.com/364355
    if ('libpng warning: iCCP: Not recognizing known sRGB profile that has '
        + 'been edited' in line):
      continue
    filtered_lines.append(line)
  return ''.join(filtered_lines)


def DidCrunchFail(returncode, stderr):
  """Determines whether aapt crunch failed from its return code and output.

  Because aapt's return code cannot be trusted, any output to stderr is
  an indication that aapt has failed (http://crbug.com/314885).
  """
  return returncode != 0 or stderr


def ZipResources(resource_dirs, zip_path):
  # Python zipfile does not provide a way to replace a file (it just writes
  # another file with the same name). So, first collect all the files to put
  # in the zip (with proper overriding), and then zip them.
  files_to_zip = dict()
  for d in resource_dirs:
    for root, _, files in os.walk(d):
      for f in files:
        archive_path = f
        parent_dir = os.path.relpath(root, d)
        if parent_dir != '.':
          archive_path = os.path.join(parent_dir, f)
        path = os.path.join(root, f)
        files_to_zip[archive_path] = path
  build_utils.DoZip(files_to_zip.iteritems(), zip_path)


def CombineZips(zip_files, output_path):
  # When packaging resources, if the top-level directories in the zip file are
  # of the form 0, 1, ..., then each subdirectory will be passed to aapt as a
  # resources directory. While some resources just clobber others (image files,
  # etc), other resources (particularly .xml files) need to be more
  # intelligently merged. That merging is left up to aapt.
  def path_transform(name, src_zip):
    return '%d/%s' % (zip_files.index(src_zip), name)

  build_utils.MergeZips(output_path, zip_files, path_transform=path_transform)


def _ExtractPackageFromManifest(manifest_path):
  doc = xml.etree.ElementTree.parse(manifest_path)
  return doc.getroot().get('package')


def _OnStaleMd5(options):
  aapt = options.aapt_path
  with build_utils.TempDir() as temp_dir:
    deps_dir = os.path.join(temp_dir, 'deps')
    build_utils.MakeDirectory(deps_dir)
    v14_dir = os.path.join(temp_dir, 'v14')
    build_utils.MakeDirectory(v14_dir)

    gen_dir = os.path.join(temp_dir, 'gen')
    build_utils.MakeDirectory(gen_dir)
    r_txt_path = os.path.join(gen_dir, 'R.txt')
    srcjar_dir = os.path.join(temp_dir, 'java')

    input_resource_dirs = options.resource_dirs

    if not options.v14_skip:
      for resource_dir in input_resource_dirs:
        generate_v14_compatible_resources.GenerateV14Resources(
            resource_dir,
            v14_dir)

    dep_zips = options.dependencies_res_zips
    dep_subdirs = []
    for z in dep_zips:
      subdir = os.path.join(deps_dir, os.path.basename(z))
      if os.path.exists(subdir):
        raise Exception('Resource zip name conflict: ' + os.path.basename(z))
      build_utils.ExtractAll(z, path=subdir)
      dep_subdirs.append(subdir)

    # Generate R.java. This R.java contains non-final constants and is used only
    # while compiling the library jar (e.g. chromium_content.jar). When building
    # an apk, a new R.java file with the correct resource -> ID mappings will be
    # generated by merging the resources from all libraries and the main apk
    # project.
    package_command = [aapt,
                       'package',
                       '-m',
                       '-M', options.android_manifest,
                       '--auto-add-overlay',
                       '--no-version-vectors',
                       '-I', options.android_sdk_jar,
                       '--output-text-symbols', gen_dir,
                       '-J', gen_dir,  # Required for R.txt generation.
                       '--ignore-assets', build_utils.AAPT_IGNORE_PATTERN]

    # aapt supports only the "--include-all-resources" mode, where each R.java
    # file ends up with all symbols, rather than only those that it had at the
    # time it was originally generated. This subtle difference makes no
    # difference when compiling, but can lead to increased unused symbols in the
    # resulting R.class files.
    # TODO(agrieve): See if proguard makes this difference actually translate
    # into a size difference. If not, we can delete all of our custom R.java
    # template code above (and make include_all_resources the default).
    if options.include_all_resources:
      srcjar_dir = gen_dir
      if options.extra_res_packages:
        colon_separated = ':'.join(options.extra_res_packages)
        package_command += ['--extra-packages', colon_separated]
      if options.non_constant_id:
        package_command.append('--non-constant-id')
      if options.custom_package:
        package_command += ['--custom-package', options.custom_package]
      if options.shared_resources:
        package_command.append('--shared-lib')
      if options.app_as_shared_lib:
        package_command.append('--app-as-shared-lib')

    for d in input_resource_dirs:
      package_command += ['-S', d]

    # Adding all dependencies as sources is necessary for @type/foo references
    # to symbols within dependencies to resolve. However, it has the side-effect
    # that all Java symbols from dependencies are copied into the new R.java.
    # E.g.: It enables an arguably incorrect usage of
    # "mypackage.R.id.lib_symbol" where "libpackage.R.id.lib_symbol" would be
    # more correct. This is just how Android works.
    for d in dep_subdirs:
      package_command += ['-S', d]

    if options.proguard_file:
      package_command += ['-G', options.proguard_file]
    build_utils.CheckOutput(package_command, print_stderr=False)

    # When an empty res/ directory is passed, aapt does not write an R.txt.
    if not os.path.exists(r_txt_path):
      build_utils.Touch(r_txt_path)

    if not options.include_all_resources:
      packages = list(options.extra_res_packages)
      r_txt_files = list(options.extra_r_text_files)

      cur_package = options.custom_package
      if not options.custom_package:
        cur_package = _ExtractPackageFromManifest(options.android_manifest)

      # Don't create a .java file for the current resource target when:
      # - no package name was provided (either by manifest or build rules),
      # - there was already a dependent android_resources() with the same
      #   package (occurs mostly when an apk target and resources target share
      #   an AndroidManifest.xml)
      if cur_package != 'dummy.package' and cur_package not in packages:
        packages.append(cur_package)
        r_txt_files.append(r_txt_path)

      if packages:
        shared_resources = options.shared_resources or options.app_as_shared_lib
        CreateRJavaFiles(srcjar_dir, r_txt_path, packages, r_txt_files,
                         shared_resources)

    # This is the list of directories with resources to put in the final .zip
    # file. The order of these is important so that crunched/v14 resources
    # override the normal ones.
    zip_resource_dirs = input_resource_dirs + [v14_dir]

    base_crunch_dir = os.path.join(temp_dir, 'crunch')

    # Crunch image resources. This shrinks png files and is necessary for
    # 9-patch images to display correctly. 'aapt crunch' accepts only a single
    # directory at a time and deletes everything in the output directory.
    for idx, input_dir in enumerate(input_resource_dirs):
      crunch_dir = os.path.join(base_crunch_dir, str(idx))
      build_utils.MakeDirectory(crunch_dir)
      zip_resource_dirs.append(crunch_dir)
      CrunchDirectory(aapt, input_dir, crunch_dir)

    ZipResources(zip_resource_dirs, options.resource_zip_out)

    if options.all_resources_zip_out:
      CombineZips([options.resource_zip_out] + dep_zips,
                  options.all_resources_zip_out)

    if options.R_dir:
      build_utils.DeleteDirectory(options.R_dir)
      shutil.copytree(srcjar_dir, options.R_dir)
    else:
      build_utils.ZipDir(options.srcjar_out, srcjar_dir)

    if options.r_text_out:
      shutil.copyfile(r_txt_path, options.r_text_out)


def main(args):
  args = build_utils.ExpandFileArgs(args)
  options = _ParseArgs(args)

  possible_output_paths = [
    options.resource_zip_out,
    options.all_resources_zip_out,
    options.proguard_file,
    options.r_text_out,
    options.srcjar_out,
  ]
  output_paths = [x for x in possible_output_paths if x]

  # List python deps in input_strings rather than input_paths since the contents
  # of them does not change what gets written to the depsfile.
  input_strings = options.extra_res_packages + [
    options.app_as_shared_lib,
    options.custom_package,
    options.include_all_resources,
    options.non_constant_id,
    options.shared_resources,
    options.v14_skip,
  ]

  input_paths = [
    options.aapt_path,
    options.android_manifest,
    options.android_sdk_jar,
  ]
  input_paths.extend(options.dependencies_res_zips)
  input_paths.extend(options.extra_r_text_files)

  resource_names = []
  for resource_dir in options.resource_dirs:
    for resource_file in build_utils.FindInDirectory(resource_dir, '*'):
      input_paths.append(resource_file)
      resource_names.append(os.path.relpath(resource_file, resource_dir))

  # Resource filenames matter to the output, so add them to strings as well.
  # This matters if a file is renamed but not changed (http://crbug.com/597126).
  input_strings.extend(sorted(resource_names))

  build_utils.CallAndWriteDepfileIfStale(
      lambda: _OnStaleMd5(options),
      options,
      input_paths=input_paths,
      input_strings=input_strings,
      output_paths=output_paths,
      # TODO(agrieve): Remove R_dir when it's no longer used (used only by GYP).
      force=options.R_dir)


if __name__ == '__main__':
  main(sys.argv[1:])
