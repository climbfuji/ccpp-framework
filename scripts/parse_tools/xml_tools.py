#!/usr/bin/env python3

"""
Parse a host-model registry XML file and return the captured variables.
"""

# Python library imports
from __future__ import print_function
import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
import xml.dom.minidom
sys.path.insert(0, os.path.dirname(__file__))
# CCPP framework imports
from parse_source import CCPPError
from parse_log import init_log, set_log_to_null

# Global data
_INDENT_STR = "  "
_XMLLINT = shutil.which('xmllint') # Blank if not installed
beg_tag_re = re.compile(r"([<][^/][^<>]*[^/][>])")
end_tag_re = re.compile(r"([<][/][^<>/]+[>])")
simple_tag_re = re.compile(r"([<][^/][^<>/]+[/][>])")

# Find python version
PYSUBVER = sys.version_info[1]
_LOGGER = None

###############################################################################
class XMLToolsInternalError(ValueError):
###############################################################################
    """Error class for reporting internal errors"""
    def __init__(self, message):
        """Initialize this exception"""
        super().__init__(message)

###############################################################################
def suite_name_str(suite, group=None, filename=None, verbose=False):
###############################################################################
    """Construct a string that describes a suite / group combination.
    An optional filename argument can be added to the string.

    >>> name_str("sname", "gname")
    'sname:gname'
    >>> name_str("sname", "gname", filename="fname")
    'sname:gname:fname'
    """
    names = [suite]
    if not group:
        group = "no_group"
    # end if
    if verbose:
        names.append(f"group {group}")
        if filename:
            names.append(f"file {filename}")
        # end if
        jstr = ", "
    else:
        names.append(group)
        if filename:
            names.append(filename)
        # end if
        jstr = ":"
    # end if
    return jstr.join(names)

###############################################################################
def element_gather_nested(element, parent, nest_chain):
###############################################################################
    """Find all nested suites in <element> and add a work item for each one.

    Parameters:
        element (xml.etree.ElementTree.Element): A group or nested_suite element
        parent (xml.etree.ElementTree.Element): The parent element (group or suite) of this element
        nest_chain (list): The dependency chain that led to this nested suite

    Returns:
        list: A list of expansion work items. A work item is a list that contains:
            - the nested suite to process
            - the suite or group that contains the nested suite
            - the chain of suite / group combinations that led to this work item.
    """

    work_items = []
    if element.tag == 'group':
        nested_suites = element.findall("nested_suite")
        work_parent = element
    elif element.tag == 'nested_suite':
        nested_suites = [element]
        work_parent = parent
    else:
        # Just ignore items such as schemes and subcycles
        nested_suites = []
    # end if
    for nested in nested_suites:
        work_items.append([nested, work_parent, nest_chain])
    # end for
    return work_items

###############################################################################
def call_command(commands, logger, silent=False):
###############################################################################
    """
    Try a command line and return the output on success (None on failure)
    >>> _LOGGER = init_log('xml_tools')
    >>> set_log_to_null(_LOGGER)
    >>> call_command(['ls', 'really__improbable_fffilename.foo'], _LOGGER) #doctest: +IGNORE_EXCEPTION_DETAIL
    Traceback (most recent call last):
    CCPPError: Execution of 'ls really__improbable_fffilename.foo' failed:
    [Errno 2] No such file or directory
    >>> call_command(['ls', 'really__improbable_fffilename.foo'], _LOGGER, silent=True)
    False
    >>> call_command(['ls'], _LOGGER)
    True
    >>> try:
    ...    call_command(['ls','--invalid-option'], _LOGGER)
    ... except CCPPError as e:
    ...    print(str(e))
    Execution of 'ls --invalid-option' failed with code: 2
    Error output: ls: unrecognized option '--invalid-option'
    Try 'ls --help' for more information.
    >>> try:
    ...    os.chdir(os.path.dirname(__file__))
    ...    call_command(['ls', os.path.basename(__file__), 'foo.bar.baz'], _LOGGER)
    ... except CCPPError as e:
    ...    print(str(e))
    Execution of 'ls xml_tools.py foo.bar.baz' failed with code: 2
    xml_tools.py
    Error output: ls: cannot access 'foo.bar.baz': No such file or directory
    """
    result = False
    outstr = ''
    try:
        cproc = subprocess.run(commands, check=True,
                                capture_output=True)
        if not silent:
            logger.debug(cproc.stdout)
        # end if
        result = cproc.returncode == 0
    except (OSError, CCPPError, subprocess.CalledProcessError) as err:
        if silent:
            result = False
        else:
            cmd = ' '.join(commands)
            outstr = f"Execution of '{cmd}' failed with code: {err.returncode}\n"
            outstr += f"{err.output.decode('utf-8', errors='replace').strip()}"
            if hasattr(err, 'stderr') and err.stderr:
                stderr_str = err.stderr.decode('utf-8', errors='replace').strip()
                if stderr_str:
                    if err.output:
                        outstr += os.linesep
                    # end if
                    outstr += f"Error output: {stderr_str}"
                # end if
            # end if
            raise CCPPError(outstr) from err
        # end if
    # end of try
    return result

###############################################################################
def find_schema_version(root):
###############################################################################
    """
    Find the version of the host registry file represented by root
    >>> find_schema_version(ET.fromstring('<model name="CAM" version="1.0"></model>'))
    [1, 0]
    >>> find_schema_version(ET.fromstring('<entry_id version="2.0"></entry_id>'))
    [2, 0]
    >>> find_schema_version(ET.fromstring('<model name="CAM" version="1.a"></model>')) #doctest: +IGNORE_EXCEPTION_DETAIL
    Traceback (most recent call last):
    CCPPError: Illegal version string, '1.a'
    Format must be <integer>.<integer>
    >>> find_schema_version(ET.fromstring('<model name="CAM" version="0.0"></model>')) #doctest: +IGNORE_EXCEPTION_DETAIL
    Traceback (most recent call last):
    CCPPError: Illegal version string, '0.0'
    Major version must be at least 1
    >>> find_schema_version(ET.fromstring('<model name="CAM" version="0.-1"></model>')) #doctest: +IGNORE_EXCEPTION_DETAIL
    Traceback (most recent call last):
    CCPPError: Illegal version string, '0.0'
    Minor version must be at least 0
    """
    verbits = None
    if 'version' not in root.attrib:
        raise CCPPError("version attribute required")
    # end if
    version = root.attrib['version']
    versplit = version.split('.')
    try:
        if len(versplit) != 2:
            raise CCPPError('oops')
        # end if (no else needed)
        try:
            verbits = [int(x) for x in versplit]
        except ValueError as verr:
            raise CCPPError(verr) from verr
        # end try
        if verbits[0] < 1:
            raise CCPPError('Major version must be at least 1')
        # end if
        if verbits[1] < 0:
            raise CCPPError('Minor version must be non-negative')
        # end if
    except CCPPError as verr:
        errstr = """Illegal version string, '{}'
        Format must be <integer>.<integer>"""
        ve_str = str(verr)
        if ve_str:
            errstr = ve_str + '\n' + errstr
        # end if
        raise CCPPError(errstr.format(version)) from verr
    # end try
    return verbits

###############################################################################
def find_schema_file(schema_root, version, schema_path=None):
###############################################################################
    """Find and return the schema file based on <schema_root> and <version>
    or return None.
    If <schema_path> is present, use that as the directory to find the
    appropriate schema file. Otherwise, just look in the current directory."""

    verstring = '_'.join([str(x) for x in version])
    schema_filename = "{}_v{}.xsd".format(schema_root, verstring)
    if schema_path:
        schema_file = os.path.join(schema_path, schema_filename)
    else:
        schema_file = schema_filename
    # end if
    if os.path.exists(schema_file):
        return schema_file
    # end if
    return None

###############################################################################
def validate_xml_file(filename, schema_root, version, logger,
                      schema_path=None, error_on_noxmllint=False):
###############################################################################
    """
    Find the appropriate schema and validate the XML file, <filename>,
    against it using xmllint
    """
    # Check the filename
    if not os.path.isfile(filename):
        raise CCPPError("validate_xml_file: Filename, '{}', does not exist".format(filename))
    # end if
    if not os.access(filename, os.R_OK):
        raise CCPPError("validate_xml_file: Cannot open '{}'".format(filename))
    # end if
    if os.path.isfile(schema_root):
        # We already have a file, just use it
        schema_file = schema_root
    else:
        if not schema_path:
            # Find the schema, based on the model version
            thispath = os.path.abspath(__file__)
            pdir = os.path.dirname(os.path.dirname(os.path.dirname(thispath)))
            schema_path = os.path.join(pdir, 'schema')
        # end if
        schema_file = find_schema_file(schema_root, version, schema_path)
        if not (schema_file and os.path.isfile(schema_file)):
            verstring = '.'.join([str(x) for x in version])
            emsg = f"""validate_xml_file: Cannot find schema for version {verstring},
            {schema_file} does not exist"""
            raise CCPPError(emsg)
        # end if
    # end if
    if not os.access(schema_file, os.R_OK):
        emsg = "validate_xml_file: Cannot open schema, '{}'"
        raise CCPPError(emsg.format(schema_file))
    # end if
    if _XMLLINT:
        logger.debug("Checking file {} against schema {}".format(filename,
                                                                 schema_file))
        cmd = [_XMLLINT, '--noout', '--schema', schema_file, filename]
        result = call_command(cmd, logger)
        return result
    # end if
    lmsg = "xmllint not found, could not validate file {}"
    if error_on_noxmllint:
        raise CCPPError("validate_xml_file: " + lmsg.format(filename))
    # end if
    logger.warning(lmsg.format(filename))
    return True # We could not check but still need to proceed

###############################################################################
def read_xml_file(filename, logger=None):
###############################################################################
    """Read the XML file, <filename>, and return its tree and root

    Parameters:
        filename (str): The path to an XML file to read and search.
        logger (logging.Logger, optional): Logger for warnings/errors.

    Returns:
        tree (xml.etree.ElementTree): The element tree from the input file.
        root (xml.etree.ElementTree.Element): The root element of tree.

    Raises:
        CCPPError: If the file cannot be found or read.
    """
    if os.path.isfile(filename) and os.access(filename, os.R_OK):
        file_open = (lambda x: open(x, 'r', encoding='utf-8'))
        with file_open(filename) as file_:
            try:
                tree = ET.parse(file_)
                root = tree.getroot()
            except ET.ParseError as perr:
                emsg = "read_xml_file: Cannot read {}, {}"
                raise CCPPError(emsg.format(filename, perr)) from perr
    elif not os.access(filename, os.R_OK):
        raise CCPPError("read_xml_file: Cannot open '{}'".format(filename))
    else:
        emsg = "read_xml_file: Filename, '{}', does not exist"
        raise CCPPError(emsg.format(filename))
    # end if
    if logger:
        logger.debug(f"Reading XML file {filename}")
    # end if
    return tree, root

###############################################################################
def load_suite_by_name(suite_name, group_name, file, logger=None):
###############################################################################
    """
    Load a suite by its name, or a group of a suite by the suite and group names.

    Parameters:
        suite_name (str): The name of the suite to find.
        group_name (str or None): The name of the group to find within the suite.
        file (str): The path to an XML file to read and search.
        logger (logging.Logger, optional): Logger for warnings/errors.

    Returns:
        xml.etree.ElementTree.Element: The matching suite or group element.

    Raises:
        CCPPError: If the suite or group is not found, or if the schema is invalid.

    Examples:
        >>> import tempfile
        >>> import xml.etree.ElementTree as ET
        >>> logger = init_log('xml_tools')
        >>> # Create temporary files for the nested suites
        >>> tmpdir = tempfile.TemporaryDirectory()
        >>> file1_path = os.path.join(tmpdir.name, "file1.xml")
        >>> # Write XML contents to temporary file
        >>> with open(file1_path, "w") as f:
        ...     _ = f.write('''
        ... <suite name="physics_suite" version="2.0">
        ...   <group name="dynamics"/>
        ...   <group name="physics"/>
        ... </suite>
        ... ''')
        >>> load_suite_by_name("physics_suite", None, file1_path, logger).tag
        'suite'
        >>> load_suite_by_name("physics_suite", "dynamics", file1_path, logger).attrib['name']
        'dynamics'
        >>> load_suite_by_name("physics_suite", "missing_group", file1_path, logger) #doctest: +IGNORE_EXCEPTION_DETAIL
        Traceback (most recent call last):
        ...
        CCPPError: Nested suite physics_suite, group missing_group, not found
        >>> load_suite_by_name("missing_suite", None, file1_path, logger) #doctest: +IGNORE_EXCEPTION_DETAIL
        Traceback (most recent call last):
        ...
        CCPPError: Nested suite missing_suite not found
        >>> tmpdir.cleanup()
    """
    _, root = read_xml_file(file, logger)
    schema_version = find_schema_version(root)
    if schema_version[0] < 2:
        raise CCPPError(f"XML schema version {schema_version} " + \
                        f"invalid for nested suite {suite_name}")
    res = validate_xml_file(file, 'suite', schema_version, logger)
    if not res:
        raise CCPPError(f"Invalid suite definition file, '{sdf}'")
    suite = root
    if suite.attrib.get("name") == suite_name:
        if group_name:
            for group in suite.findall("group"):
                if group.attrib.get("name") == group_name:
                    return group
        else:
            return suite
    emsg = f"Nested suite {suite_name}" \
         + (f", group {group_name}," if group_name else "") \
         + " not found" + (f" in file {file}" if file else "")
    raise CCPPError(emsg)

###############################################################################
def replace_nested_suite(element, nested_suite, nest_chain, default_path, logger):
###############################################################################
    """
    Replace a <nested_suite> tag with the actual suite or group it references.

    This function looks up a referenced suite or suite group from an external
    file, deep copies its children, and replaces the <nested_suite> element
    in the parent `element` with the copied contents.

    Parameters:
        element (xml.etree.ElementTree.Element): The parent element containing the nested suite.
        nested_suite (xml.etree.ElementTree.Element): The <nested_suite> element to be replaced.
        nest_chain(list): The list of suite / group combinations leading to <nested_suite>
        default_path (str): The default path to look for nested SDFs if file is not a absolute path.
        logger (logging.Logger or None): Logger to record debug information.

    Returns:
        list: A list of expansion work items contained in this suite

    Example:
        >>> import tempfile
        >>> import xml.etree.ElementTree as ET
        >>> from types import SimpleNamespace
        >>> logger = init_log('xml_tools')
        >>> tmpdir = tempfile.TemporaryDirectory()
        >>> file1_path = os.path.join(tmpdir.name, "file1.xml")
        >>> with open(file1_path, "w") as f:
        ...     _ = f.write('''
        ... <suite name="my_suite" version="2.0">
        ...   <group name="my_group">
        ...     <scheme>my_scheme</scheme>
        ...   </group>
        ... </suite>
        ... ''')
        >>> # Import nested suite at suite level
        >>> xml = f'''
        ... <suite name="top" version="2.0">
        ...   <nested_suite name="my_suite" file="{file1_path}"/>
        ... </suite>
        ... '''
        >>> top_suite = ET.fromstring(xml)
        >>> nested = top_suite.find("nested_suite")
        >>> replace_nested_suite(top_suite, nested, ["depend"], tmpdir.name, logger)
        'my_suite'
        >>> [child.tag for child in top_suite]
        ['group']
        >>> top_suite.find("group").find("scheme").text
        'my_scheme'
        >>> # Import group from nested suite at group level
        >>> xml = f'''
        ... <suite name="top" version="2.0">
        ...   <group name="top_group">
        ...     <nested_suite name="my_suite" group="my_group" file="{file1_path}"/>
        ...   </group>
        ... </suite>
        ... '''
        >>> top_suite = ET.fromstring(xml)
        >>> top_group = top_suite.find("group")
        >>> nested = top_group.find("nested_suite")
        >>> replace_nested_suite(top_group, nested, ["depend"], tmpdir.name, logger)
        'my_suite'
        >>> [child.tag for child in top_suite]
        ['group']
        >>> top_suite.find("group").find("scheme").text
        'my_scheme'
        >>> # Import group from nested suite at suite level
        >>> xml = f'''
        ... <suite name="top" version="2.0">
        ...   <nested_suite name="my_suite" group="my_group" file="{file1_path}"/>
        ... </suite>
        ... '''
        >>> top_suite = ET.fromstring(xml)
        >>> nested = top_suite.find("nested_suite")
        >>> replace_nested_suite(top_suite, nested, ["depend"], tmpdir.name, logger)
        'my_suite'
        >>> [child.tag for child in top_suite]
        ['group']
        >>> top_suite.find("group").find("scheme").text
        'my_scheme'
        >>> tmpdir.cleanup()
    """
    suite_name = nested_suite.attrib.get("name")
    group_name = nested_suite.attrib.get("group")
    file = nested_suite.attrib.get("file")
    new_parent = suite_name_str(suite_name, group=group_name, filename=file)
    if new_parent in nest_chain:
        nest_chain.append(new_parent)
        depj = " ==> "
        raise CCPPError(f"Circular dependency in nested suites: {depj.join(nest_chain)}")
    # end if
    nest_chain.append(new_parent)
    if not os.path.isabs(file):
        file = os.path.abspath(os.path.join(default_path, file))
    # end if
    referenced_suite = load_suite_by_name(suite_name, group_name, file,
                                          logger=logger)
    imported_content = [ET.fromstring(ET.tostring(child))
                        for child in referenced_suite]
    work_items = []
    # Swap nested suite with imported content
    for item in imported_content:
        # If we are inserting a nested suite at the suite level (element.tag is suite),
        # but we only want one group (group_name is not none), then we need to wrap
        # the item in a group element. If on the other hand we insert an entire suite
        # (all groups) at the suite level, or a specific group at the group level,
        # then we can insert the item as is.
        if element.tag == 'suite' and group_name:
            item_to_insert = ET.Element("group", attrib={"name": group_name})
            item_to_insert.append(item)
            new_parent = item_to_insert
        else:
            item_to_insert = item
            new_parent = element
        # end if
        work_items.extend(element_gather_nested(item, new_parent, nest_chain))
# XXgoldyXX: v debug only
        if nested_suite not in list(element):
            nsname = nested_suite.attrib.get("name")
            elname = element.attrib.get("name")
            raise XMLToolsInternalError(f"Trying to insert {nested_suite.tag}, {nsname} in {element.tag}, {elname}")
# XXgoldyXX: ^ debug only
        element.insert(list(element).index(nested_suite), item_to_insert)
    # end for
    element.remove(nested_suite)
    if logger:
        msg = f"Expanded nested suite '{suite_name}'" \
            + (f", group '{group_name}'," if group_name else "") \
            + (f" in file '{file}'" if file else "")
        logger.debug(msg.rstrip(','))
    # Return the list of new work items contained in this suite or group
    return work_items

###############################################################################
def expand_nested_suites(suite, default_path, logger=None):
###############################################################################
    """
    Recursively expand all <nested_suite> elements within the XML <suite> element.

    This function finds <nested_suite> elements within <group> or <suite> elements,
    and replaces them with the corresponding content from another suite.

    This operation is recursive and will continue expanding until no <nested_suite>
    elements remain.

    Parameters:
        suite (xml.etree.ElementTree.Element): The root <suite> element.
        logger (logging.Logger, optional): Logger for debug messages.

    Returns:
        None. The XML tree is modified in place.

    Example:
        >>> import tempfile
        >>> import xml.etree.ElementTree as ET
        >>> logger = init_log('xml_tools')
        >>> tmpdir = tempfile.TemporaryDirectory()
        >>> file1_path = os.path.join(tmpdir.name, "file1.xml")
        >>> file2_path = os.path.join(tmpdir.name, "file2.xml")
        >>> file3_path = os.path.join(tmpdir.name, "file3.xml")
        >>> file4_path = os.path.join(tmpdir.name, "file4.xml")
        >>> file5_path = os.path.join(tmpdir.name, "file5.xml")
        >>> # Write mock XML contents for the nested suites
        >>> with open(file1_path, "w") as f:
        ...     _ = f.write('''
        ... <suite name="microphysics_suite" version="2.0">
        ...   <group name="micro">
        ...     <scheme>cloud_scheme</scheme>
        ...   </group>
        ... </suite>
        ... ''')
        >>> with open(file2_path, "w") as f:
        ...     _ = f.write('''
        ... <suite name="pbl_suite" version="2.0">
        ...   <group name="pbl">
        ...     <scheme>pbl_scheme</scheme>
        ...   </group>
        ... </suite>
        ... ''')
        >>> with open(file3_path, "w") as f:
        ...     _ = f.write('''
        ... <suite name="rad_suite" version="2.0">
        ...   <group name="radlw">
        ...     <scheme>rrtmg_lw_scheme</scheme>
        ...   </group>
        ...   <group name="radsw">
        ...     <scheme>rrtmg_sw_scheme</scheme>
        ...   </group>
        ... </suite>
        ... ''')
        >>> with open(file4_path, "w") as f:
        ...     _ = f.write(f'''
        ... <suite name="pbl_suite1" version="2.0">
        ...   <nested_suite name="pbl_suite2" file="{file5_path}"/>
        ... </suite>
        ... ''')
        >>> with open(file5_path, "w") as f:
        ...     _ = f.write(f'''
        ... <suite name="pbl_suite2" version="2.0">
        ...   <nested_suite name="pbl_suite1" file="{file4_path}"/>
        ... </suite>
        ... ''')
        >>> # Parent suite
        >>> xml_content = f'''
        ... <suite name="physics_suite" version="2.0">
        ...   <group name="main">
        ...     <nested_suite name="microphysics_suite" group="micro" file="{file1_path}"/>
        ...   </group>
        ...   <nested_suite name="pbl_suite" file="{file2_path}"/>
        ...   <nested_suite name="rad_suite" group_name="radlw" file="{file3_path}"/>
        ... </suite>
        ... '''
        >>> suite = ET.fromstring(xml_content)
        >>> expand_nested_suites(suite, tmpdir.name, logger)
        >>> ET.dump(suite)
        <suite name="physics_suite" version="2.0">
          <group name="main">
            <scheme>cloud_scheme</scheme></group>
          <group name="pbl">
            <scheme>pbl_scheme</scheme>
          </group><group name="radlw">
            <scheme>rrtmg_lw_scheme</scheme>
          </group><group name="radsw">
            <scheme>rrtmg_sw_scheme</scheme>
          </group></suite>
        >>> # Test infite recursion
        >>> xml_content = f'''
        ... <suite name="physics_suite">
        ...   <group name="main">
        ...     <nested_suite name="microphysics_suite" group="micro" file="{file1_path}"/>
        ...   </group>
        ...   <nested_suite name="pbl_suite1" file="{file4_path}"/>
        ... </suite>
        ... '''
        >>> suite = ET.fromstring(xml_content)
        >>> expand_nested_suites(suite, tmpdir.name, logger) #doctest: +IGNORE_EXCEPTION_DETAIL
        Traceback (most recent call last):
        ...
        CCPPError: Exceeded number of iterations while expanding nested suites
        >>> tmpdir.cleanup()
    """
    # To avoid infinite recursion, keep track of the suites processed and flag
    # an error if a repeat is found.
    # Each item in the worklist is a nested suite to process, the suite or group
    # that contains the nested suite, and the chain of
    # suite / group combinations that led to this work item.
    worklist = []
    suite_name = suite_name_str(suite.attrib.get("name"))
    for item in suite:
        worklist.extend(element_gather_nested(item, suite, []))
    # end for
    while worklist:
        # Process each item in the worklist
        work_item = worklist.pop(0)
        nested = work_item[0]
        element = work_item[1]
        nest_chain = work_item[2]
        worklist.extend(replace_nested_suite(element, nested, nest_chain, default_path, logger))
    # end for

###############################################################################
def write_xml_file(root, file_path, logger=None):
###############################################################################
    """Pretty-prints element root to an ASCII file using xml.dom.minidom"""

    def remove_whitespace_nodes(node):
        """Helper function to recursively remove all text nodes that contain
        only whitespace, which eliminates blank lines in the output."""
        for child in list(node.childNodes):
            if child.nodeType == child.TEXT_NODE and not child.data.strip():
                node.removeChild(child)
            elif child.hasChildNodes():
                remove_whitespace_nodes(child)

    # Convert ElementTree to a byte string
    byte_string = ET.tostring(root, 'us-ascii')

    # Parse string using minidom for pretty printing
    reparsed = xml.dom.minidom.parseString(byte_string)

    # Clean whitespace-only text nodes
    remove_whitespace_nodes(reparsed)

    # Generate pretty-printed XML string
    pretty_xml = reparsed.toprettyxml(indent="  ")

    # Write to file
    with open(file_path, 'w', errors='xmlcharrefreplace') as f:
        f.write(pretty_xml)

    # Tell everyone!
    if logger:
        logger.debug(f"Writing XML file {file_path}")

##############################################################################
