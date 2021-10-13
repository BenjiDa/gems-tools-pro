# GeMS_CreateDatabase_Arc10.1.py
#   Python script to create an empty NCGMP09-style
#   ArcGIS 10 geodatabase for geologic map data
#
#   Ralph Haugerud, USGS
#
# RUN AS TOOLBOX SCRIPT FROM ArcCatalog OR ArcMap

# 9 Sept 2016: Made all fields NULLABLE
# 19 Dec 2016: Added GeoMaterialsDict table, domains
# 8 March 2017: Added  ExistenceConfidence, IdentityConfidence, ScientificConfidence domains, definitions, and definitionsource
# 17 March 2017  Added optional table MiscellaneousMapInformation
# 30 Oct 2017  Moved CartoRepsAZGS and GeMS_lib.gdb to ../gems-resources
# 4 March 2018  changed to use writeLogfile()
# 16 May 2019 GeMS_CreateDatabase_Arc10.py Python 2.7 ported to Python 3 to work in ArcGIS Pro 2.1, Evan Thoms	

# 8 June 2020 In transDict (line 31), changed 'NoNulls':'NON_NULLABLE' to 'NoNulls':'NULLABLE'
#   " "       Fixed bug with addTracking(), where EnableEditorTracking_management apparently wants in_dataset to be a full pathname
#   " "       Added MapUnitLines to list of feature classes that could be created (line 153)
# 28 Sept 2020 Now defines coordinate system for CMU and cross section feature datasets (= map coordinate system)
# 7 Oct 2020 Improved definition of cross section feature classes to match specification
# Edits 10/8/20 to update to Ralph's latest changes (above), Evan Thoms
# 23 December 2020: Changed how MapUnitPoints feature class is created, so that it follows definition in GeMS_Definitions.py - RH                                                                                                                          

import arcpy, sys, os
import re
from pathlib import Path
from GeMS_Definition import tableDict, GeoMaterialConfidenceValues, DefaultExIDConfidenceValues, IDLength
from GeMS_utilityFunctions import *
import copy       

versionString = 'GeMS_CreateDatabase_AGP2.py, version of 7 October 2021'
rawurl = 'https://raw.githubusercontent.com/usgs/gems-tools-pro/master/Scripts/GeMS_CreateDatabase_AGP2.py'
checkVersion(versionString, rawurl, 'gems-tools-pro')

debug = True

default = '#'

transDict =     { 'String': 'TEXT',
                  'Single': 'FLOAT',
                  'Double': 'DOUBLE',
                  'NoNulls':'NULLABLE', #NB-enforcing NoNulls at gdb level creates headaches; instead, check while validating
                  'NullsOK':'NULLABLE',
                  'Optional':'NULLABLE',                                        
                  'Date'  : 'DATE'  }
                  
# set up the parameter variables as global so they can be accessed throughout the script

def eval_bool(param):
    '''Return a boolean for various possibilities of boolean-like values'''
    return_bool = False
    if param in [1, True, '1', 'yes', 'Yes', 'true', 'True']:
        return_bool = True
 
    return return_bool

def addMsgAndPrint(msg, severity=0): 
    # prints msg to screen and adds msg to the geoprocessor (in case this is run as a tool) 
    print(msg)
    try: 
        for string in msg.split('\n'): 
            # Add appropriate geoprocessing message 
            if severity == 0: 
                arcpy.AddMessage(string) 
            elif severity == 1: 
                arcpy.AddWarning(string) 
            elif severity == 2: 
                arcpy.AddError(string) 
    except: 
        pass 

def createFeatureClass(gdb, featureDataSet, featureClass, shapeType, fieldDefs):
    addMsgAndPrint(f'    Creating feature class {featureClass}...')
    try:
        arcpy.env.workspace = str(gdb)
        arcpy.CreateFeatureclass_management(featureDataSet, featureClass, shapeType)
        thisFC = str(gdb / featureDataSet / featureClass)
        for fDef in fieldDefs:
            try:
                if fDef[1] == 'String':
                    # note that we are ignoring fDef[2],  NullsOK or NoNulls
                    arcpy.AddField_management(thisFC, fDef[0], transDict[fDef[1]], '#', '#', fDef[3], '#', 'NULLABLE')
                else:
                     # note that we are ignoring fDef[2], NullsOK or NoNulls
                    arcpy.AddField_management(thisFC, fDef[0], transDict[fDef[1]], '#', '#', '#', '#', 'NULLABLE')
            except:
                addMsgAndPrint(f'Failed to add field {fDef[0]} to feature class {featureClass}')
                addMsgAndPrint(arcpy.GetMessages(2))
    except :
        addMsgAndPrint(arcpy.GetMessages())
        addMsgAndPrint(f'Failed to create feature class {featureClass} in dataset {featureDataSet}')
        
def addTracking(tfc):
    if arcpy.Exists(tfc):
        addMsgAndPrint(f'    Enabling edit tracking in {tfc}')
        try:
            arcpy.EnableEditorTracking_management(tfc, 'created_user', 'created_date', 'last_edited_user', 'last_edited_date', 'ADD_FIELDS', 'DATABASE_TIME')
        except:
            addMsgAndPrint(tfc)
            addMsgAndPrint(arcpy.GetMessages(2))
    
def rename_field(defs, start_name, end_name):
    """renames a field in a list generated from tableDict for
    special cases; CrossSections and OrientationPoints
    instead of using AlterField after creation which was throwing errors"""
    f_list = copy.deepcopy(defs)
    list_item = [n for n in f_list if n[0] == start_name] #finds ['MapUnitPolys_ID', 'String', 'NoNulls', 50], for instance
    i = f_list.index(list_item[0]) #finds the index of that item
    f_list[i][0] = end_name #changes the name in the list
    arcpy.AddMessage(f'{f_list[i][0]} becoming {end_name}')
    arcpy.AddMessage(f_list)
    return f_list                                                   

def main(thisDB, coordSystem, OptionalElements, nCrossSections, trackEdits, addLTYPE, addConfs):
    # create feature dataset GeologicMap
    addMsgAndPrint('  Creating feature dataset GeologicMap...')
    try:
        arcpy.CreateFeatureDataset_management(str(thisDB), 'GeologicMap', coordSystem)
    except:
        addMsgAndPrint(arcpy.GetMessages(2))

    # create feature classes in GeologicMap
    # poly feature classes
    featureClasses = ['MapUnitPolys']
    for fc in ['DataSourcePolys', 'MapUnitOverlayPolys', 'OverlayPolys']:
        if fc in OptionalElements:
            featureClasses.append(fc)
    for featureClass in featureClasses:
        fieldDefs = tableDict[featureClass]
        if addLTYPE and fc != 'DataSourcePolys':
            fieldDefs.append(['PTYPE', 'String', 'NullsOK', 200])
        createFeatureClass(thisDB, 'GeologicMap', featureClass, 'POLYGON', fieldDefs)
            
    # line feature classes
    featureClasses = ['ContactsAndFaults']
    for fc in ['GeologicLines', 'CartographicLines', 'IsoValueLines', 'MapUnitLines']:
        if fc in OptionalElements:
            featureClasses.append(fc)

    for featureClass in featureClasses:
        fieldDefs = tableDict[featureClass]
        if featureClass in ['ContactsAndFaults', 'GeologicLines'] and addLTYPE:
            fieldDefs.append(['LTYPE', 'String', 'NullsOK', 200])
        createFeatureClass(thisDB, 'GeologicMap', featureClass, 'POLYLINE', fieldDefs)

    # point feature classes
    featureClasses = []
    for fc in ['OrientationPoints', 'GeochronPoints', 'FossilPoints', 'Stations',
                  'GenericSamples', 'GenericPoints',  'MapUnitPoints']:
        if fc in OptionalElements:
            featureClasses.append(fc)
    for featureClass in featureClasses:
        """
The following block of code was bypassing the MapUnitPoints definition now in GeMS_Definitions.py and
appending PTYPE to the resulting feature class, along with the PTTYPE field appended in the 
next statement. I think we don't need it, but need to talk with Evan about this.
If he concurs, will delete this block.
Ralph Haugerud
23 December 2020
I agree - 
        # the following if statement used to be here, but was removed at some point
        # putting it back to allow for creation of MapUnitPoints after discussion
        # with Luke Blair - Evan Thoms
        if featureClass == 'MapUnitPoints': 
            fieldDefs = tableDict['MapUnitPolys']
            if addLTYPE:
                fieldDefs.append(['PTYPE','String','NullsOK',50])
        else:	
            fieldDefs = tableDict[featureClass]
            if addLTYPE and featureClass in ['OrientationPoints']:
                fieldDefs.append(['PTTYPE','String','NullsOK',50])
        # end of re-inserted if statement   
        """
        fieldDefs = tableDict[featureClass]                                           
        if addLTYPE:
            fieldDefs.append(['PTTYPE', 'String', 'NullsOK', 50])
        createFeatureClass(thisDB, 'GeologicMap', featureClass, 'POINT', fieldDefs)

    # create feature dataset CorrelationOfMapUnits
    if 'CorrelationOfMapUnits' in OptionalElements:
        addMsgAndPrint('  Creating feature dataset CorrelationOfMapUnits...')
        arcpy.CreateFeatureDataset_management(str(thisDB), 'CorrelationOfMapUnits', coordSystem)
        fieldDefs = tableDict['CMUMapUnitPolys']
        createFeatureClass(thisDB, 'CorrelationOfMapUnits', 'CMUMapUnitPolys', 'POLYGON', fieldDefs)
        fieldDefs = tableDict['CMULines']
        createFeatureClass(thisDB, 'CorrelationOfMapUnits', 'CMULines', 'POLYLINE', fieldDefs)
        fieldDefs = tableDict['CMUPoints']
        createFeatureClass(thisDB, 'CorrelationOfMapUnits', 'CMUPoints', 'POINT', fieldDefs)
    
    # create CrossSections
    if nCrossSections > 26:
        nCrossSections = 26
    if nCrossSections < 0:
        nCrossSections = 0
    # note space in position 0
    alphabet = ' ABCDEFGHIJKLMNOPQRSTUVWXYZ'
    
    for n in range(1, nCrossSections+1):
        xsLetter = alphabet[n]
        xsName = f'CrossSection{xsLetter}'
        xsN = f'CS{xsLetter}'
        addMsgAndPrint(f'  Creating feature data set CrossSection{xsLetter}...')
        arcpy.CreateFeatureDataset_management(str(thisDB), xsName, coordSystem)
        muDefs = rename_field(tableDict['MapUnitPolys'], 'MapUnitPolys_ID', f'{xsN}MapUnitPolys_ID')

        createFeatureClass(thisDB, xsName, f'{xsN}MapUnitPolys', 'POLYGON', muDefs)
        cfDefs = rename_field(tableDict['ContactsAndFaults'], 'ContactsAndFaults_ID', f'{xsN}ContactsAndFaults_ID')
        createFeatureClass(thisDB, xsName, f'{xsN}ContactsAndFaults', 'POLYLINE', cfDefs)


        if 'OrientationPoints' in OptionalElements:
            opDefs = rename_field(tableDict['OrientationPoints'], 'OrientationPoints_ID', f'{xsN}OrientationPoints_ID')
            createFeatureClass(thisDB, xsName, f'{xsN}OrientationPoints', 'POINT', opDefs)
  

    # create tables
    tables = ['DescriptionOfMapUnits', 'DataSources', 'Glossary']
    for tb in ['RepurposedSymbols', 'StandardLithology', 'GeologicEvents', 'MiscellaneousMapInformation']:
        if tb in OptionalElements:
            tables.append(tb)
    for table in tables:
        addMsgAndPrint(f'  Creating table {table}...')
        try:
            arcpy.CreateTable_management(str(thisDB), table)
            fieldDefs = tableDict[table]
            for fDef in fieldDefs:
                try:
                    if fDef[1] == 'String':
                        arcpy.AddField_management(str(thisDB / table), fDef[0], transDict[fDef[1]], '#', '#', fDef[3], '#', transDict[fDef[2]])
                    else:
                        arcpy.AddField_management(str(thisDB / table), fDef[0], transDict[fDef[1]], '#', '#', '#', '#', transDict[fDef[2]])
                except:
                    addMsgAndPrint(f'Failed to add field {fDef[0]} to table {table}')
                    addMsgAndPrint(arcpy.GetMessages(2))		    
        except:
            addMsgAndPrint(arcpy.GetMessages())

    ### GeoMaterials
    addMsgAndPrint('  Setting up GeoMaterialsDict table and domains...')
    
    # import GeoMaterials table from csv in \resources
    toolbox_dir = Path(__file__).parent
    geo_mat_table = toolbox_dir / 'resources' / 'geomaterialdict.csv'
    arcpy.MakeTableView_management(str(geo_mat_table), 'geomatdict')
    arcpy.conversion.TableToTable('geomatdict', str(thisDB), 'GeoMaterialDict')
    
    # make GeoMaterials domain
    arcpy.TableToDomain_management(str(thisDB / 'GeoMaterialDict'), 'GeoMaterial', 'IndentedName', str(thisDB), 'GeoMaterials')
    
    # attach it to DMU field GeoMaterial
    arcpy.AssignDomainToField_management(str(thisDB / 'DescriptionOfMapUnits'), 'GeoMaterial', 'GeoMaterials')  
    
    # Make GeoMaterialConfs domain, attach it to DMU field GeoMaterialConf
    arcpy.CreateDomain_management(str(thisDB), 'GeoMaterialConfidenceValues', '', 'TEXT', 'CODED')
    for val in GeoMaterialConfidenceValues:
        arcpy.AddCodedValueToDomain_management(str(thisDB), 'GeoMaterialConfidenceValues', val, val)
    arcpy.AssignDomainToField_management(str(thisDB / 'DescriptionOfMapUnits'), 'GeoMaterialConfidence', 'GeoMaterialConfidenceValues')
    
     #onfidence domains, Glossary entries, and DataSources entry
    if addConfs:
        addMsgAndPrint('  Adding standard ExistenceConfidence and IdentityConfidence domains')
        # create domain, add domain values, and link domain to appropriate fields
        addMsgAndPrint('    Creating domain, linking domain to appropriate fields')
        arcpy.CreateDomain_management(str(thisDB), 'ExIDConfidenceValues', '', 'TEXT', 'CODED')
        for item in DefaultExIDConfidenceValues:  # items are [term, definition, source]
            code = item[0]
            arcpy.AddCodedValueToDomain_management(str(thisDB), 'ExIDConfidenceValues', code, code)
        arcpy.env.workspace = str(thisDB)
        dataSets = arcpy.ListDatasets()
        for ds in dataSets:
            arcpy.env.workspace = str(thisDB / ds)
            fcs = arcpy.ListFeatureClasses()
            for fc in fcs:
                fieldNames = fieldNameList(fc)
                for fn in fieldNames:
                    if fn in ('ExistenceConfidence', 'IdentityConfidence', 'ScientificConfidence'):
                        #addMsgAndPrint('    '+ds+'/'+fc+':'+fn)
                        arcpy.AssignDomainToField_management(str(thisDB / ds / fc), fn, 'ExIDConfidenceValues')
        # add definitions of domain values to Glossary
        addMsgAndPrint('    Adding domain values to Glossary')
        ## create insert cursor on Glossary
        cursor = arcpy.da.InsertCursor(str(thisDB / 'Glossary'), ['Term', 'Definition', 'DefinitionSourceID'])
        for item in DefaultExIDConfidenceValues:
            cursor.insertRow((item[0], item[1], item[2]))
        del cursor
        # add definitionsource to DataSources
        addMsgAndPrint('    Adding definition source to DataSources')        
        ## create insert cursor on DataSources
        cursor = arcpy.da.InsertCursor(str(thisDB / 'DataSources'), ['DataSources_ID', 'Source', 'URL'])
        cursor.insertRow(('FGDC-STD-013-2006', 'Federal Geographic Data Committee [prepared for the Federal Geographic Data Committee by the U.S. Geological Survey], 2006, FGDC Digital Cartographic Standard for Geologic Map Symbolization: Reston, Va., Federal Geographic Data Committee Document Number FGDC-STD-013-2006, 290 p., 2 plates.','https://ngmdb.usgs.gov/fgdc_gds/geolsymstd.php'))
        del cursor 

    # trackEdits, add editor tracking to all feature classes and tables
    if trackEdits:
        arcpy.env.workspace = str(thisDB)
        tables = arcpy.ListTables()
        datasets = arcpy.ListDatasets()
        for dataset in datasets:
            addMsgAndPrint(f'  Dataset {dataset}')
            arcpy.env.workspace = str(thisDB / dataset)
            fcs = arcpy.ListFeatureClasses()
            for fc in fcs:
                if trackEdits:
                    addTracking(str(thisDB / aTable))
        if trackEdits:
            addMsgAndPrint('  Tables ')
            arcpy.env.workspace = thisDB
            for aTable in tables:
                if aTable != 'GeoMaterialDict':
                    addTracking(str(thisDB / aTable))

def createDatabase(outputDir, thisDB):
    addMsgAndPrint(f'  Creating geodatabase {thisDB}')
    if arcpy.Exists(str(outputDir / thisDB)):
        addMsgAndPrint(f'  Geodatabase {thisDB} already exists.')
        addMsgAndPrint('   forcing exit with error')
        raise arcpy.ExecuteError
    try:
        # removed check for mdb. Personal geodatabases are out - ET
        if thisDB[-4:] == '.gdb':
            arcpy.CreateFileGDB_management(str(outputDir), thisDB)
        return True
    except:
        addMsgAndPrint(f'Failed to create geodatabase {str(Path(outputDir) / thisDB)}')
        addMsgAndPrint(arcpy.GetMessages(2))
        return False


def parse_parameters(params):
    '''Usage:
       GeMS_CreateDatabase_Arc10.1.py [directory] [geodatabaseName] [coordSystem]
                    [OptionalElements] [#XSections] [AddEditTracking] [AddRepresentations] [AddLTYPE]
       [directory] Name of a directory. Must exist and be writable.
       [geodatabaseName] Name of gdb to be created, with or without .gdb extension.
       [coordSystem] May select an ESRI projection file, import a spatial reference from an existing dataset, or 
          define a new spatial reference system from scratch.
       [OptionalElements] List of optional feature classes to add to GDB, e.g.,
          [OrientationPoints, CartographicLines, RepurposedSymbols]. May be empty.
       [#XSections] is an integer (0, 1, 2, ...) specifying the intended number of
          cross-sections
       [AddEditTracking] Enables edit tracking on all feature classes. Adds fields created_user, 
          created_date, last_edited_user, and last_edited_date. Dates are recorded in database (local) time. 
          Default is checked. This parameter is ignored if the installed version of ArcGIS is less than 10.1.
       [AddLTYPE] If true, add LTYPE field to feature classes ContactsAndFaults and GeologicLines, add PTTYPE field
          to feature class OrientationData, and add PTTYPE field to MapUnitLabelPoints 
       [AddCONF] If true: 1) Attaches standard values of "certain" and "questionable" as a coded-value domain to 
          all ExistenceConfidence, IdentityConfidence, and ScientificConfidence fields. 2) Adds definitions and 
          definition source for "certain" and "questionable" to the Glossary table. 3) Adds the definition source 
          (FGDC-STD-013-2006) to the DataSources table.

        Boolean parameters may be set with 1, "1", "0" "true", "false",True, "yes", "Yes",  
    '''

    trackEdits = False
    addLTYPE = True
    addConfs = True
    
    addMsgAndPrint('Starting script')
    addMsgAndPrint(versionString)
    
    outputDir = params[0]
    if outputDir == '#':
        outputDir = os.getcwd()
    outputDir = Path(outputDir) 
    
    db_name = params[1]
    if not db_name.endswith('.gdb'):
        db_name = f'{db_name}.gdb'

    coordSystem = params[2]

    if type(params[3]) == list:
        OptionalElements = params[3]
    elif type(params[3]) == str:
        if (params[3]) == "#" or (params[3]) == "":
            OptionalElements = []
        else:
            OptionalElements = re.split(r"[-;,.\s]\s*", params[3])
            
    nCrossSections = int(params[4])

    trackEdits = eval_bool(params[5])
  
    if arcpy.GetInstallInfo()['Version'] < '10.1':
        trackEdits = False
    
    addLTYPE = eval_bool(params[6])
   
    addConfs = eval_bool(params[7])
 
    # create gdb in output directory and run main routine
    if createDatabase(outputDir, db_name):
        thisDB = Path(outputDir) / db_name
        main(thisDB, coordSystem, OptionalElements, nCrossSections, trackEdits, addLTYPE, addConfs)

    # try to write a readme within the .gdb
    if str(thisDB).endswith('.gdb'):
        try:
            writeLogfile(str(thisDB), f'Geodatabase created by {versionString}')
        except:
            addMsgAndPrint(f'Failed to write to {str(thisDB)}/00log.txt')
    
#########################################
# if this script is being called from the command line, __name__ gets set to '__main__' and the parameters
# are accessed through sys.argv (although first we check to see if the right number have been supplied and
# remind the user through a docstring if not)
# if the script is being accessed after being imported into another script, eg, the GeMS python toolbox .pyt, then the parameters
# will be collected by that script and sent directly to def parse_parameters()
if __name__ == '__main__':
    if len(sys.argv) < 6:
        print(parse_parameters.__doc__)
    else:
        parse_parameters(sys.argv[1:])

