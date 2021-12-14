# Script to step through an identified subset of feature classes in GeologicMap feature dataset
# and, for specified values of independent fields, calculate values of dependent fields.
# Useful for translating Alacarte-derived data into NCGMP09 format, and for using NCGMP09
# to digitize data in Alacarte mode.
#
# Edited 30 May 2019 by Evan Thoms:
#   Updated to work with Python 3 in ArcGIS Pro2
#   Ran script through 2to3 to fix minor syntax issues
#   Manually edited the rest to make string building for messages
#   and whereClauses more pythonic
#   Added better handling of boolean to determine overwriting or not of existing values

import arcpy, sys
from GeMS_utilityFunctions import *

versionString = 'GeMS_AttributeByKeyValues_AGP2.py, version of 28 October 2021'
rawurl = 'https://raw.githubusercontent.com/usgs/gems-tools-pro/master/Scripts/GeMS_AttributeByKeyValues_AGP2.py'
checkVersion(versionString, rawurl, 'gems-tools-pro')

separator = '|'

def eval_bool(param):
    '''Return a boolean for various possibilities of boolean-like values'''
    return_bool = False
    if param in [1, '1', True, 'true', 'True', 'yes', 'Yes', 'on', 'On', 'hella']:
        return_bool = True
        
    return return_bool

def makeFieldTypeDict(fc_path):
    fdict = {}
    fields = arcpy.ListFields(fc_path)
    for fld in fields:
        fdict[fld.name] = fld.type
    return fdict

def main(parameters):
    """
    Usage: 
      GeMS_AttributeByKeyValues.py <geodatabase> <file.txt> <force calculation>
      <geodatabase> is an GeMS-style geodatabase with map data in feature
         dataset GeologicMap
      <file.txt> is a formatted text file that specifies feature classes,
         field names, values of independent fields, and values of dependent fields.
         See Dig24K_KeyValues.txt for an example and format instructions.
      <force calculation> boolean (yes, no, true, false, etc.) that will 
         determine if existing values may be overwritten (True) or only null, or 0
         otherwise empty values will be calculated (False)
     """
    addMsgAndPrint('  '+versionString)

    gdb = parameters[0]
    key_val_file = parameters[1]
    forceCalc = eval_bool(parameters[2])

    if forceCalc:
        addMsgAndPrint("Forcing the overwriting of existing values")
    
    # make a dictionary of {geodatabase objects: full paths}
    gdb_walk = arcpy.da.Walk(gdb)
    gdb_dict = {}
    for workspace, fds, filenames in gdb_walk:
        for filename in filenames:
            gdb_dict[filename] = os.path.join(workspace, filename)
    
    # from the key-value text file, make a dictionary of 
    # feature class name = all of the related value mappings
    # first, open the file, read the lines as a list, and clean the strings
    with open(key_val_file, 'r') as file:
        key_lines = file.readlines()
    key_lines = [line for line in key_lines if not line.startswith('#')]
    key_lines = [line.strip() for line in key_lines if not line.strip() == '']
    key_lines = [line.replace(' | ', '|') for line in key_lines]
    
    # read through this list of lines and save the indices of the feature class names
    # and the last item in the list will be the length of the list
    fc_indices = []
    for i, line in enumerate(key_lines):
        terms = line.split('|')
        if len(terms) == 1:
            fc_indices.append(i)
    fc_indices.append(len(key_lines))
    
    # now we have some indices within the list we can use to slice the list
    # into chunks that group lines related to the feature classes
    # build a dictionary in the form {table: {independent field: [dependent field values]}}
    # also build a dictionary in the form {table: [update field names]} to store the names of the dependent fields
    fc_val_dict = {}
    update_fields_dict = {}
    n = 0
    while n < len(fc_indices) - 1:
        # the table name is at index n from fc_indices
        fc_name = key_lines[fc_indices[n]]
        # the values lines follow from index n to the next number in fc_indices
        fc_vals = [n for n in key_lines[fc_indices[n] + 1 : fc_indices[n + 1]]]
        # the names of the fields will be at index 0 of this new list of lines (fc_vals_)
        update_fields = fc_vals[0].split('|')
        update_fields_dict[fc_name] = update_fields
        # use dictionary comprehension to make entries {independent field: [dependent field values]}
        # {key:value for item in list}
        val_dict = {n.split('|')[0]:n.split('|')[1:] for n in fc_vals[1:]}
        fc_val_dict[fc_name] = val_dict
        n = n + 1
    
    # now work through the tables that are named in the key-value text file
    for fc in fc_val_dict:
        addMsgAndPrint(f"Parsing values in {fc}")
        update_fields = update_fields_dict[fc]
        update_val_dict = fc_val_dict[fc]
        if fc in gdb_dict:
            with arcpy.da.UpdateCursor(gdb_dict[fc], update_fields) as cursor:
                for i, row in enumerate(cursor):
                    # the independent field will be at index row[0] and we can use it as a key in the dictionary we just
                    # retrieved for this particular feature class, update_val_dict, to get a list of values for the rest of the fields
                    update_vals = update_val_dict[row[0].strip()]
                    for n, k in enumerate(update_vals):
                        # if forceCalc is true, write the value regardless of what may already be
                        # in the attribute table
                        if forceCalc:
                            row[n+1] = k
                        # but if forceCalc is false, only write a value if the cell is empty
                        else:
                            if row[n+1].strip() is None:
                                row[n+1] = k
                    try:
                        cursor.updateRow(row)
                    except Exception as error:
                        addMsgAndPrint(f"Failed to update row {i+1}")
                        addMsgAndPrint(error)

#########################################
# if this script is being called from the command line, __name__ gets set to '__main__' and the parameters
# are accessed through sys.argv (although first we check to see if the right number have been supplied and
# remind the user through a docstring if not)
# if the script is being accessed after being imported into another script, eg, the GeMS python toolbox .pyt, then the parameters
# will be collected by that script and sent directly to def main()
if __name__ == '__main__':
    if len(sys.argv) < 4:
        print(main.__doc__)
    else:
        main(sys.argv[1:])