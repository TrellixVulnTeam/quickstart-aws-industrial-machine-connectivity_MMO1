#!/usr/bin/env python3
import collections.abc
from datetime import date, datetime
import json
import logging
import os
import re
import sys
import time

import boto3
from botocore.exceptions import ClientError

log = logging.getLogger('assetModelConverter')
log.setLevel(logging.DEBUG)


class IgnitionFileDriver:
    def __init__(self):
        self.boto3Session = boto3.Session()
        self.dynamo = self.boto3Session.resource('dynamodb')
        self.assetModelTable = self.dynamo.Table(os.environ['DynamoDB_Model_Table'])
        self.assetTable = self.dynamo.Table(os.environ['DynamoDB_Asset_Table'])

        self.saveAMCData = os.environ.get('SaveAMCData', False)

        # Max depth of placeholder models to create, usually set to the max depth that sitewise will allow.
        self.hierarchyMaxDepth = 10

        # This is the property alias prefix.
        # TODO In the future this may use a configurable value to uniquely identify data coming from a specific
        #      instance of ignition.
        self.tagAliasPrefix = '/Tag Providers/default'

        self.dataTypeTable = {
            "Int4": "INTEGER",
            "Int8": "INTEGER",
            "Int16": "INTEGER",
            "Int32": "INTEGER",
            "Int64": "INTEGER",
            "Float4": "DOUBLE",
            "Double": "DOUBLE",
            "Boolean": "BOOLEAN",
            "String": "STRING",
            "DateTime": "INTEGER"
        }
        self.unsupportedDataTypes = ['Template']
        self.tagBlacklist = ['Sim Controls']

        # Timestamp format to use when printing or writing messages/file/folder names
        self.timestampFormat = '%Y-%m-%d_%H-%M-%S'

        self.rawData = {}
        # models stores all of our model structure
        self.models = {}
        # used to store our asset structure
        self.assets = []
        # depth model map has our mapping by depth to the place holder models
        self.depthModelMap = {}

        self.modelPropertyMap = {}

        self.normalizedModels = {}
        self.normalizedAssets = {}

    @staticmethod
    def jsonSerial(obj):
        """
        This is used when converting dictionary data to JSON and vice versa. Used to support types that are not
        supported in the JSON format.
        :param obj:
        :return:
        """
        if isinstance(obj, (datetime, date)):
            return obj.isoformat()
        raise TypeError("Type %s not serializable" % type(obj))

    def updateDict(self, baseData, updateData):
        """
        Updates the baseData dictionary with the contents of updateData.
        :param baseData:
        :param updateData:
        :return:
        """
        for key, value in updateData.items():
            if isinstance(value, collections.abc.Mapping):
                baseData[key] = self.updateDict(baseData.get(key, {}), value)
            else:
                baseData[key] = value

        return baseData

    def buildStructure(self, topicList, value):
        """
        Un-squashes a path structure from the birth message topic.
        :param topicList:
        :param value:
        :return:
        """
        if not topicList:
            return value

        pathSeg = topicList.pop(0)
        childNode = self.buildStructure(topicList, value)

        return {pathSeg: childNode}

    def processBirthObjects(self, birthData):
        for birth in birthData:
            self.updateDict(self.rawData, birth)

        for record in self.rawData['tags']:
            if record['name'] in self.tagBlacklist:
                continue

            if record['name'] == '_types_':
                for modelTag in record['tags']:
                    if modelTag['tagType'] == 'UdtType':
                        self.models[modelTag['name']] = modelTag
            else:
                self.assets.append(record)

    def saveData(self, data, filename):
        """
        Writes a dictionary to a file as JSON.
        :param data:
        :param filename:
        :return:
        """
        with open(filename, 'w', encoding='utf-8') as dataFile:
            dataFile.write(json.dumps(data, indent=4, sort_keys=True, default=self.jsonSerial))

    def loadData(self, filename):
        """
        Loads a JSON file into a dictionary.
        :param filename:
        :return:
        """
        with open(filename, 'r', encoding='utf-8') as dataFile:
            return json.load(dataFile)

    def genModelNode(self, name, modelMetrics, parentName='root'):
        modelNode = {
            "assetModelName": name,
            "parent": parentName,
            "assetModelProperties": [],
            "assetModelHierarchies": [],
            "change": 'YES'
        }

        self.modelPropertyMap[name] = {}

        for metric in modelMetrics:
            if 'dataType' not in metric:
                continue

            if metric['dataType'] in self.unsupportedDataTypes:
                continue

            opcItemPath = metric['opcItemPath']['binding']
            self.modelPropertyMap[name][metric['name']] = opcItemPath

            modelNode['assetModelProperties'].append({
                'name': metric['name'],
                'dataType': self.dataTypeTable.get(metric['dataType']),
                'type': {
                    'measurement': {}
                },
            })

        return modelNode

    def generatePlaceholderModels(self, depthLevel=0, parentName='root'):
        """
        By depth level, creates placeholder models. These are used to store asset structure folders.
        :param depthLevel:
        :return:
        """
        if depthLevel >= self.hierarchyMaxDepth:
            return None

        if depthLevel == 1:
            pName = '__Node'
        elif depthLevel == 0:
            pName = '__Group'
        else:
            pName = f'__DeviceLevel{depthLevel-1}'

        modelNode = self.genModelNode(
            name=pName,
            modelMetrics=[],
            parentName=parentName,
        )

        self.normalizedModels[pName] = modelNode
        self.depthModelMap[depthLevel] = modelNode

        self.generatePlaceholderModels(depthLevel=depthLevel + 1, parentName=pName)

    def getAssetNodeType(self, nodeValue):
        """
        This is how we decide if a given node is an actual asset instance, or merely folder structure
        :param nodeValue:
        :return:
        """
        # if 'isDefinition' in nodeValue and 'reference' in nodeValue:
        if nodeValue['tagType'] == 'UdtInstance':
            return 'asset'
        else:
            return 'folder'

    def getAssetNodeData(self, nodeValue, depthLevel):
        """
        Uses the nodeValue of the asset level tree and the current depth to return the nodeType and base model reference.
        :param nodeValue:
        :param depthLevel:
        :return:
        """
        nodeType = self.getAssetNodeType(nodeValue)

        if nodeType == 'asset':
            # self.genModelNode()
            referenceName = nodeValue['typeId']
            derivedModelName = referenceName + f'_D{depthLevel}'

            if derivedModelName not in self.normalizedModels:
                self.normalizedModels[derivedModelName] = self.genModelNode(
                    name=derivedModelName,
                    modelMetrics=self.models[referenceName]['tags'],
                )

            baseModelName = derivedModelName
        else:
            baseModelName = self.depthModelMap[depthLevel]['assetModelName']

        return nodeType, baseModelName

    # def genAssetNode(self, nodeName, baseModel, tagsList, parentName=''):
    def genAssetNode(self, nodeName, nodeValue, baseModel, parentName=''):
        assetNode = {
            'assetName': nodeName,
            'modelName': baseModel,
            'change': 'YES',
            'tags': [],
        }

        modelNode = self.normalizedModels[baseModel]
        tagsList = modelNode.get('assetModelProperties')

        if tagsList and 'parameters' in nodeValue:
            parameters = nodeValue['parameters']

            for tag in tagsList:
                # tagPath = tag['properties']['ConfiguredTagPath']['value']
                # tagPath = tag['__opcItemPath'].format(tag['parameters']).split(';')[1]
                tagPath = self.modelPropertyMap[baseModel][tag['name']].format(**parameters).split(';')[1]

                tagEntry = {
                    'tagName': tag['name'],
                    'tagPath': re.sub('^s=\[.+\]', self.tagAliasPrefix + '/', tagPath)
                }
                assetNode['tags'].append(tagEntry)

        if parentName:
            assetNode['parentName'] = parentName

        return assetNode

    def processAssetTree(self, nodeValue, depthLevel=0, parentPath=''):
        """
        Recursively walks self.assets, creating assets, and their associated property aliases where relevant.
        :param nodeName:
        :param nodeValue:
        :param depthLevel:
        :param parentPath:
        :return:
        """
        nodeName = nodeValue['name']
        nodePath = parentPath + '/' + nodeName
        log.info(nodePath)
        nodeType, baseModelName = self.getAssetNodeData(nodeValue, depthLevel)

        # metricsList = None
        # if 'metrics' in nodeValue:
        #     metricsList = nodeValue['metrics']
        assetNode = self.genAssetNode(
            nodeName=nodePath,
            nodeValue=nodeValue,
            baseModel=baseModelName,
            # tagsList=metricsList,
            parentName=parentPath,
        )
        self.normalizedAssets[nodePath] = assetNode

        # Process child nodes
        if nodeType == 'folder':
            # for childName, childValue in nodeValue.items():
            for childValue in nodeValue['tags']:
                self.processAssetTree(
                    # nodeName=childName,
                    nodeValue=childValue,
                    depthLevel=depthLevel+1,
                    parentPath=nodePath,
                )

    def createDynamoRecords(self, table, data, primaryKey):
        for record in data:
            try:
                # log.info(record)
                table.put_item(Item=record, ConditionExpression=f'attribute_not_exists({primaryKey})')
                time.sleep(0.1)

            except ClientError as cErr:
                if cErr.response['Error']['Code'] == 'ConditionalCheckFailedException':
                    log.info('Ignoring existing record {}'.format(record[primaryKey]))
                else:
                    raise

    def processEvent(self, event):
        # log.info(event)

        self.processBirthObjects(event['birthData'])

        if self.saveAMCData:
            self.saveData(self.assets, 'assetsRaw.json')
            self.saveData(self.models, 'modelsRaw.json')
            self.saveData(self.rawData, 'dataRaw.json')

        try:
            self.generatePlaceholderModels()

            for assetGroup in self.assets:
                self.processAssetTree(assetGroup)

            dynamoAssets = [value for value in self.normalizedAssets.values()]
            dynamoModels = [value for value in self.normalizedModels.values()]
            # if self.saveAMCData:
            #     self.saveData(self.assets, 'assets.json')
            #     self.saveData(self.models, 'models.json')

            if self.saveAMCData:
                self.saveData(dynamoAssets, 'assets.json')
                self.saveData(dynamoModels, 'models.json')

            self.createDynamoRecords(self.assetModelTable, dynamoModels, 'assetModelName')
            self.createDynamoRecords(self.assetTable, dynamoAssets, 'assetName')

        except ClientError as cErr:
            log.exception('Failed to process birth objects')


def handler(event, context):
    IgnitionFileDriver().processEvent(event)
