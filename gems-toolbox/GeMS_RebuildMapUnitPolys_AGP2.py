"""GeMS_RebuildMapUnits
A simpler version of the GeMS Make Polys tool
1) Will not build MapUnitPolys from scratch. Use the ArcGIS Feature to Polygon tool for that.
2) Does not concatenate a label points feature class from map units converted to points
   and an optional label points feature class. It uses one or the other.
3) No error checking between polygon and optional label points attributes. 
   Use colored label points for that.
   
Use this tool in ArcMap while editing ContactsAndFaults linework to quickly rebuild the 
MapUnitPolygons feature class as you change the shape of polygons or want to add new ones.
"""

import arcpy
import os
import sys
import re
from GeMS_utilityFunctions import *

versionString = 'GeMS_RebuildMapUnits_Arc10.py, version of 14 April 2021'
rawurl = 'https://raw.githubusercontent.com/usgs/gems-tools-arcmap/master/gems-toolbox/GeMS_RebuildMapUnits_Arc10.py'

def get_trailing_number(s):
    m = re.search(r'\d+$', s)
    return int(m.group()) if m else 0

def findLyr(lname):
    aprx = arcpy.mp.ArcGISProject('CURRENT')
    active_map = aprx.activeMap
    lList = active_map.listLayers()
    for lyr in lList:
        if lyr.longName == lname:
            pos = lList.index(lyr)
            if pos == 0:
                refLyr = lList[pos + 1]
                insertPos = "BEFORE"
            else:
                refLyr = lList[pos - 1]
                insertPos = "AFTER"
                
            return [lyr, active_map, refLyr, insertPos]
            
#********************************************************************************************
def main(parameters):
    checkVersion(versionString, rawurl, 'gems-tools-arcmap')
    
    #Get the parameters
    lineLayer = parameters[0]
    polyLayer = parameters[1]
    labelPoints = parameters[2]
    saveMUP = parameters[3]

    #collect the findLyr properties
    lyrProps = findLyr(polyLayer)
    lyr = lyrProps[0]                               #the layer object
    am = lyrProps[1]                                #the data frame within which the layer resides
    refLyr = lyrProps[2]                            #a layer above or below which the layer resides
    insertPos = lyrProps[3]                         #index above or below the reference layer
    newPolys = lyr.dataSource                       #the path to the dataSource of the polygon layer
    discName = os.path.basename(lyr.dataSource)     #the name in the geodatabase of the datasource
    con_props = lyr.connectionProperties
    db_path = con_props['source']['connection_info']['database']

    # save a temporary layer file for the polygons to save rendering and other settings
    # including joins to other tables
    # .lyr is saved to the folder of the geodatabase
    # lyr.workspacePath returnd the gdb, not a feature dataset
    lyrPath = os.path.join(os.path.dirname(db_path), lyr.name + '.lyrx')
    if arcpy.Exists(lyrPath):
        os.remove(lyrPath)
            
    addMsgAndPrint("  saving " + polyLayer + ' to ' + lyrPath)
    arcpy.management.SaveToLayerFile(lyr, lyrPath, "RELATIVE")

    #set the workspace variable to the workspace of the feature class
    #and get the name of the feature dataset
    dsPath = os.path.dirname(newPolys)
    arcpy.env.workspace = dsPath
    
    #remove join if one is there
    try:
        arcpy.management.RemoveJoin(lyr)
    except:
        pass
        
    #remove relate if one is there 
    try:
        arcpy.management.RemoveRelate(lyr)
    except:
        pass

    # make a labelPoints feature class if one was not provided
    if labelPoints in ['#', '', None]:
        #create the labelPoints name
        labelPoints = discName + '_tempLabels'
        testAndDelete(labelPoints)

        #check for an old copy of labelpoints
        if arcpy.Exists(labelPoints):
            arcpy.management.Delete(labelPoints)
        
        #create points from the attributed polygons
        arcpy.management.FeatureToPoint(lyr.dataSource, labelPoints, 'INSIDE')

    #and now remove the layer from the map
    am.removeLayer(lyr)

    #save a copy of the polygons fc or delete
    if saveMUP == 'true':
        # get new name
        pfcs = arcpy.ListFeatureClasses(discName + "*", "Polygon")
        maxN = 0
        for pfc in pfcs:
            try:
                n = int(get_trailing_number(pfc))
                if n > maxN:
                    maxN = n
            except:
                pass
        oldPolys = lyr.dataSource + str(maxN + 1)
        addMsgAndPrint("  saving " + polyLayer + ' to ' + oldPolys)
       
        try:
            oldPolysPath = os.path.join(dsPath, oldPolys)
            arcpy.management.Copy(lyr.dataSource, oldPolysPath, "FeatureClass")
        except:
            addMsgAndPrint("  arcpy.Copy_management(mup,oldPolys) failed. Maybe you need to close ArcMap?")
            sys.exit()


    addMsgAndPrint("  deleting " + newPolys)
    arcpy.management.Delete(newPolys)
    addMsgAndPrint("  recreating " + newPolys + " from new linework")

    # select all unconcealed lines
    if 'IsConcealed' in [f.name for f in arcpy.ListFields(lineLayer)]:
        where = '"IsConcealed"  NOT IN (\'Y\',\'y\')'
    else:
        where = '#'

    arcpy.SelectLayerByAttribute_management(lineLayer, "NEW_SELECTION", where)
    arcpy.management.FeatureToPolygon(lineLayer, newPolys, '#', '#', labelPoints)
    #arcpy.RefreshCatalog(arcpy.env.workspace)
    arcpy.SelectLayerByAttribute_management(lineLayer, "CLEAR_SELECTION")

    # add the layer file 
    addMsgAndPrint("  adding " + lyrPath + " to the map")
    addLyr = arcpy.mp.LayerFile(lyrPath)
    am.insertLayer(refLyr, addLyr, insertPos)

if __name__ == '__main__':
    main(sys.argv[1:])
