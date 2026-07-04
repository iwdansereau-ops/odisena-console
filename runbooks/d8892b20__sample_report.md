# DynamoDB IAM Policy Refactor Report

- Worst risk in original policy: **HIGH**
- Validation backend used: **offline**
- Overall validation OK: **True**
- Statements: original=4, refactored=11

## 1. Security findings on the ORIGINAL policy

| Severity | Code | Stmt | Sid | Message |
|----------|------|------|-----|---------|
| HIGH | `ACTION_DDB_STAR` | 0 | OverlyBroad | Action "dynamodb:*" grants every DynamoDB permission including DeleteTable and DeleteBackup. Enumerate the specific actions the workload needs. |
| HIGH | `RESOURCE_STAR` | 0 | OverlyBroad | Resource "*" grants the listed actions across every DynamoDB resource in the account. Replace with specific table/index/stream ARNs. |
| MEDIUM | `NON_DDB_ACTIONS` | 1 | MixedWithS3 | Statement mixes non-DynamoDB actions: ['s3:GetObject']. Split into a separate statement or policy for clarity. |
| HIGH | `RESOURCE_STAR` | 1 | MixedWithS3 | Resource "*" grants the listed actions across every DynamoDB resource in the account. Replace with specific table/index/stream ARNs. |
| INFO | `RESOURCE_STAR_REQUIRED` | 2 | ServiceLevel | Resource "*" is required for these service-level list/describe actions and is acceptable here. |
| MEDIUM | `UNKNOWN_ACTION` | 3 | TypoAction | Action 'dynamodb:GetItm' is not a recognized DynamoDB action (possible typo). |
| HIGH | `RESOURCE_STAR` | 3 | TypoAction | Resource "*" grants the listed actions across every DynamoDB resource in the account. Replace with specific table/index/stream ARNs. |

## 2. Access Analyzer / structural validation on the REFACTORED policy

| Source | Severity | Code | Message |
|--------|----------|------|---------|
| offline | SUGGESTION | `NO_CONDITION_ON_DESTRUCTIVE` | Destructive actions allowed without any Condition (e.g., aws:SourceIp, aws:MultiFactorAuthPresent). Consider hardening. |
| offline | SUGGESTION | `NO_CONDITION_ON_DESTRUCTIVE` | Destructive actions allowed without any Condition (e.g., aws:SourceIp, aws:MultiFactorAuthPresent). Consider hardening. |
| offline | SECURITY_WARNING | `RESOURCE_STAR` | Resource "*" grants the listed actions across every DynamoDB resource in the account. Replace with specific table/index/stream ARNs. |
| offline | SECURITY_WARNING | `DESTRUCTIVE_ON_STAR` | Destructive actions ['dynamodb:PurchaseReservedCapacityOfferings'] allowed on Resource "*". This lets the principal delete or overwrite ANY table/backup in the account. |
| offline | SUGGESTION | `NO_CONDITION_ON_DESTRUCTIVE` | Destructive actions allowed without any Condition (e.g., aws:SourceIp, aws:MultiFactorAuthPresent). Consider hardening. |
| offline | SUGGESTION | `NON_DDB_ACTIONS` | Statement mixes non-DynamoDB actions: ['s3:GetObject']. Split into a separate statement or policy for clarity. |
| offline | INFO | `RESOURCE_STAR_REQUIRED` | Resource "*" is required for these service-level list/describe actions and is acceptable here. |
| offline | SUGGESTION | `UNKNOWN_ACTION` | Action 'dynamodb:GetItm' is not a recognized DynamoDB action (possible typo). |

## 3. Action → Resource matrix for the REFACTORED policy

Every allowed DynamoDB action and the exact ARNs it is limited to.

| Sid | Effect | Class | Action | Conditioned | Resources |
|-----|--------|-------|--------|-------------|-----------|
| OverlyBroad_Table | Allow | read | `dynamodb:BatchGetItem` | no | `arn:aws:dynamodb:us-east-1:111122223333:table/Orders`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Customers` |
| OverlyBroad_Table | Allow | destructive | `dynamodb:BatchWriteItem` | no | `arn:aws:dynamodb:us-east-1:111122223333:table/Orders`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Customers` |
| OverlyBroad_Table | Allow | read | `dynamodb:ConditionCheckItem` | no | `arn:aws:dynamodb:us-east-1:111122223333:table/Orders`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Customers` |
| OverlyBroad_Table | Allow | write/admin | `dynamodb:CreateTableReplica` | no | `arn:aws:dynamodb:us-east-1:111122223333:table/Orders`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Customers` |
| OverlyBroad_Table | Allow | destructive | `dynamodb:DeleteItem` | no | `arn:aws:dynamodb:us-east-1:111122223333:table/Orders`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Customers` |
| OverlyBroad_Table | Allow | destructive | `dynamodb:DeleteTable` | no | `arn:aws:dynamodb:us-east-1:111122223333:table/Orders`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Customers` |
| OverlyBroad_Table | Allow | write/admin | `dynamodb:DeleteTableReplica` | no | `arn:aws:dynamodb:us-east-1:111122223333:table/Orders`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Customers` |
| OverlyBroad_Table | Allow | read | `dynamodb:DescribeContinuousBackups` | no | `arn:aws:dynamodb:us-east-1:111122223333:table/Orders`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Customers` |
| OverlyBroad_Table | Allow | read | `dynamodb:DescribeContributorInsights` | no | `arn:aws:dynamodb:us-east-1:111122223333:table/Orders`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Customers` |
| OverlyBroad_Table | Allow | read | `dynamodb:DescribeKinesisStreamingDestination` | no | `arn:aws:dynamodb:us-east-1:111122223333:table/Orders`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Customers` |
| OverlyBroad_Table | Allow | read | `dynamodb:DescribeTable` | no | `arn:aws:dynamodb:us-east-1:111122223333:table/Orders`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Customers` |
| OverlyBroad_Table | Allow | read | `dynamodb:DescribeTableReplicaAutoScaling` | no | `arn:aws:dynamodb:us-east-1:111122223333:table/Orders`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Customers` |
| OverlyBroad_Table | Allow | read | `dynamodb:DescribeTimeToLive` | no | `arn:aws:dynamodb:us-east-1:111122223333:table/Orders`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Customers` |
| OverlyBroad_Table | Allow | write/admin | `dynamodb:DisableKinesisStreamingDestination` | no | `arn:aws:dynamodb:us-east-1:111122223333:table/Orders`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Customers` |
| OverlyBroad_Table | Allow | write/admin | `dynamodb:EnableKinesisStreamingDestination` | no | `arn:aws:dynamodb:us-east-1:111122223333:table/Orders`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Customers` |
| OverlyBroad_Table | Allow | write/admin | `dynamodb:ExportTableToPointInTime` | no | `arn:aws:dynamodb:us-east-1:111122223333:table/Orders`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Customers` |
| OverlyBroad_Table | Allow | read | `dynamodb:GetItem` | no | `arn:aws:dynamodb:us-east-1:111122223333:table/Orders`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Customers` |
| OverlyBroad_Table | Allow | read | `dynamodb:GetRecords` | no | `arn:aws:dynamodb:us-east-1:111122223333:table/Orders`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Customers` |
| OverlyBroad_Table | Allow | write/admin | `dynamodb:ImportTable` | no | `arn:aws:dynamodb:us-east-1:111122223333:table/Orders`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Customers` |
| OverlyBroad_Table | Allow | read | `dynamodb:ListTagsOfResource` | no | `arn:aws:dynamodb:us-east-1:111122223333:table/Orders`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Customers` |
| OverlyBroad_Table | Allow | destructive | `dynamodb:PartiQLDelete` | no | `arn:aws:dynamodb:us-east-1:111122223333:table/Orders`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Customers` |
| OverlyBroad_Table | Allow | write/admin | `dynamodb:PartiQLInsert` | no | `arn:aws:dynamodb:us-east-1:111122223333:table/Orders`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Customers` |
| OverlyBroad_Table | Allow | read | `dynamodb:PartiQLSelect` | no | `arn:aws:dynamodb:us-east-1:111122223333:table/Orders`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Customers` |
| OverlyBroad_Table | Allow | write/admin | `dynamodb:PartiQLUpdate` | no | `arn:aws:dynamodb:us-east-1:111122223333:table/Orders`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Customers` |
| OverlyBroad_Table | Allow | write/admin | `dynamodb:PutItem` | no | `arn:aws:dynamodb:us-east-1:111122223333:table/Orders`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Customers` |
| OverlyBroad_Table | Allow | read | `dynamodb:Query` | no | `arn:aws:dynamodb:us-east-1:111122223333:table/Orders`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Customers` |
| OverlyBroad_Table | Allow | destructive | `dynamodb:RestoreTableFromAwsBackup` | no | `arn:aws:dynamodb:us-east-1:111122223333:table/Orders`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Customers` |
| OverlyBroad_Table | Allow | destructive | `dynamodb:RestoreTableFromBackup` | no | `arn:aws:dynamodb:us-east-1:111122223333:table/Orders`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Customers` |
| OverlyBroad_Table | Allow | read | `dynamodb:Scan` | no | `arn:aws:dynamodb:us-east-1:111122223333:table/Orders`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Customers` |
| OverlyBroad_Table | Allow | write/admin | `dynamodb:TagResource` | no | `arn:aws:dynamodb:us-east-1:111122223333:table/Orders`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Customers` |
| OverlyBroad_Table | Allow | write/admin | `dynamodb:UntagResource` | no | `arn:aws:dynamodb:us-east-1:111122223333:table/Orders`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Customers` |
| OverlyBroad_Table | Allow | write/admin | `dynamodb:UpdateContinuousBackups` | no | `arn:aws:dynamodb:us-east-1:111122223333:table/Orders`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Customers` |
| OverlyBroad_Table | Allow | write/admin | `dynamodb:UpdateContributorInsights` | no | `arn:aws:dynamodb:us-east-1:111122223333:table/Orders`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Customers` |
| OverlyBroad_Table | Allow | write/admin | `dynamodb:UpdateItem` | no | `arn:aws:dynamodb:us-east-1:111122223333:table/Orders`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Customers` |
| OverlyBroad_Table | Allow | write/admin | `dynamodb:UpdateKinesisStreamingDestination` | no | `arn:aws:dynamodb:us-east-1:111122223333:table/Orders`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Customers` |
| OverlyBroad_Table | Allow | destructive | `dynamodb:UpdateTable` | no | `arn:aws:dynamodb:us-east-1:111122223333:table/Orders`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Customers` |
| OverlyBroad_Table | Allow | write/admin | `dynamodb:UpdateTableReplicaAutoScaling` | no | `arn:aws:dynamodb:us-east-1:111122223333:table/Orders`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Customers` |
| OverlyBroad_Table | Allow | write/admin | `dynamodb:UpdateTimeToLive` | no | `arn:aws:dynamodb:us-east-1:111122223333:table/Orders`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Customers` |
| OverlyBroad_Index | Allow | read | `dynamodb:Query` | no | `arn:aws:dynamodb:us-east-1:111122223333:table/Orders/index/by_email`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Customers/index/by_email` |
| OverlyBroad_Index | Allow | read | `dynamodb:Scan` | no | `arn:aws:dynamodb:us-east-1:111122223333:table/Orders/index/by_email`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Customers/index/by_email` |
| OverlyBroad_Stream | Allow | read | `dynamodb:DescribeStream` | no | `arn:aws:dynamodb:us-east-1:111122223333:table/Orders/stream/*`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Customers/stream/*` |
| OverlyBroad_Stream | Allow | read | `dynamodb:GetRecords` | no | `arn:aws:dynamodb:us-east-1:111122223333:table/Orders/stream/*`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Customers/stream/*` |
| OverlyBroad_Stream | Allow | read | `dynamodb:GetShardIterator` | no | `arn:aws:dynamodb:us-east-1:111122223333:table/Orders/stream/*`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Customers/stream/*` |
| OverlyBroad_Backup | Allow | write/admin | `dynamodb:CreateBackup` | no | `arn:aws:dynamodb:us-east-1:111122223333:table/Orders/backup/*`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Customers/backup/*` |
| OverlyBroad_Backup | Allow | destructive | `dynamodb:DeleteBackup` | no | `arn:aws:dynamodb:us-east-1:111122223333:table/Orders/backup/*`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Customers/backup/*` |
| OverlyBroad_Backup | Allow | read | `dynamodb:DescribeBackup` | no | `arn:aws:dynamodb:us-east-1:111122223333:table/Orders/backup/*`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Customers/backup/*` |
| OverlyBroad_Backup | Allow | destructive | `dynamodb:RestoreTableFromBackup` | no | `arn:aws:dynamodb:us-east-1:111122223333:table/Orders/backup/*`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Customers/backup/*` |
| OverlyBroad_Export | Allow | read | `dynamodb:DescribeExport` | no | `arn:aws:dynamodb:us-east-1:111122223333:table/Orders/export/*`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Customers/export/*` |
| OverlyBroad_Export | Allow | write/admin | `dynamodb:ExportTableToPointInTime` | no | `arn:aws:dynamodb:us-east-1:111122223333:table/Orders/export/*`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Customers/export/*` |
| OverlyBroad_GlobalTable | Allow | write/admin | `dynamodb:CreateGlobalTable` | no | `arn:aws:dynamodb::111122223333:global-table/Orders`<br>`arn:aws:dynamodb::111122223333:global-table/Customers` |
| OverlyBroad_GlobalTable | Allow | read | `dynamodb:DescribeGlobalTable` | no | `arn:aws:dynamodb::111122223333:global-table/Orders`<br>`arn:aws:dynamodb::111122223333:global-table/Customers` |
| OverlyBroad_GlobalTable | Allow | read | `dynamodb:DescribeGlobalTableSettings` | no | `arn:aws:dynamodb::111122223333:global-table/Orders`<br>`arn:aws:dynamodb::111122223333:global-table/Customers` |
| OverlyBroad_GlobalTable | Allow | write/admin | `dynamodb:UpdateGlobalTable` | no | `arn:aws:dynamodb::111122223333:global-table/Orders`<br>`arn:aws:dynamodb::111122223333:global-table/Customers` |
| OverlyBroad_GlobalTable | Allow | write/admin | `dynamodb:UpdateGlobalTableSettings` | no | `arn:aws:dynamodb::111122223333:global-table/Orders`<br>`arn:aws:dynamodb::111122223333:global-table/Customers` |
| OverlyBroad_ServiceLevel | Allow | write/admin | `dynamodb:CreateTable` | no | `*` |
| OverlyBroad_ServiceLevel | Allow | write/admin | `dynamodb:DescribeEndpoints` | no | `*` |
| OverlyBroad_ServiceLevel | Allow | read | `dynamodb:DescribeLimits` | no | `*` |
| OverlyBroad_ServiceLevel | Allow | read | `dynamodb:DescribeReservedCapacity` | no | `*` |
| OverlyBroad_ServiceLevel | Allow | read | `dynamodb:DescribeReservedCapacityOfferings` | no | `*` |
| OverlyBroad_ServiceLevel | Allow | read | `dynamodb:ListBackups` | no | `*` |
| OverlyBroad_ServiceLevel | Allow | read | `dynamodb:ListContributorInsights` | no | `*` |
| OverlyBroad_ServiceLevel | Allow | read | `dynamodb:ListExports` | no | `*` |
| OverlyBroad_ServiceLevel | Allow | read | `dynamodb:ListGlobalTables` | no | `*` |
| OverlyBroad_ServiceLevel | Allow | read | `dynamodb:ListImports` | no | `*` |
| OverlyBroad_ServiceLevel | Allow | read | `dynamodb:ListStreams` | no | `*` |
| OverlyBroad_ServiceLevel | Allow | read | `dynamodb:ListTables` | no | `*` |
| OverlyBroad_ServiceLevel | Allow | destructive | `dynamodb:PurchaseReservedCapacityOfferings` | no | `*` |
| MixedWithS3_Table | Allow | read | `dynamodb:GetItem` | no | `arn:aws:dynamodb:us-east-1:111122223333:table/Orders`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Customers` |
| MixedWithS3_Table | Allow | read | `dynamodb:Scan` | no | `arn:aws:dynamodb:us-east-1:111122223333:table/Orders`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Customers` |
| MixedWithS3_Table | Allow | other | `s3:GetObject` | no | `arn:aws:dynamodb:us-east-1:111122223333:table/Orders`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Customers` |
| MixedWithS3_Index | Allow | read | `dynamodb:Scan` | no | `arn:aws:dynamodb:us-east-1:111122223333:table/Orders/index/by_email`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Customers/index/by_email` |
| ServiceLevel_ServiceLevel | Allow | read | `dynamodb:DescribeLimits` | no | `*` |
| ServiceLevel_ServiceLevel | Allow | read | `dynamodb:ListTables` | no | `*` |
| TypoAction_Table | Allow | write/admin | `dynamodb:GetItm` | no | `arn:aws:dynamodb:us-east-1:111122223333:table/Orders`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Customers` |

## Diff: original vs refactored

- Actions in original: **64**, in refactored: **64**
- Actions narrowed off `"*"`: **51**
- (action, resource) pairs removed: **51**
- (action, resource) pairs added: **112**

### Narrowed off `"*"`

| Action | From | To |
|--------|------|-----|
| `dynamodb:BatchGetItem` | `*` | `arn:aws:dynamodb:us-east-1:111122223333:table/Customers`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Orders` |
| `dynamodb:BatchWriteItem` | `*` | `arn:aws:dynamodb:us-east-1:111122223333:table/Customers`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Orders` |
| `dynamodb:ConditionCheckItem` | `*` | `arn:aws:dynamodb:us-east-1:111122223333:table/Customers`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Orders` |
| `dynamodb:CreateBackup` | `*` | `arn:aws:dynamodb:us-east-1:111122223333:table/Customers/backup/*`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Orders/backup/*` |
| `dynamodb:CreateGlobalTable` | `*` | `arn:aws:dynamodb::111122223333:global-table/Customers`<br>`arn:aws:dynamodb::111122223333:global-table/Orders` |
| `dynamodb:CreateTableReplica` | `*` | `arn:aws:dynamodb:us-east-1:111122223333:table/Customers`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Orders` |
| `dynamodb:DeleteBackup` | `*` | `arn:aws:dynamodb:us-east-1:111122223333:table/Customers/backup/*`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Orders/backup/*` |
| `dynamodb:DeleteItem` | `*` | `arn:aws:dynamodb:us-east-1:111122223333:table/Customers`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Orders` |
| `dynamodb:DeleteTable` | `*` | `arn:aws:dynamodb:us-east-1:111122223333:table/Customers`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Orders` |
| `dynamodb:DeleteTableReplica` | `*` | `arn:aws:dynamodb:us-east-1:111122223333:table/Customers`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Orders` |
| `dynamodb:DescribeBackup` | `*` | `arn:aws:dynamodb:us-east-1:111122223333:table/Customers/backup/*`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Orders/backup/*` |
| `dynamodb:DescribeContinuousBackups` | `*` | `arn:aws:dynamodb:us-east-1:111122223333:table/Customers`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Orders` |
| `dynamodb:DescribeContributorInsights` | `*` | `arn:aws:dynamodb:us-east-1:111122223333:table/Customers`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Orders` |
| `dynamodb:DescribeExport` | `*` | `arn:aws:dynamodb:us-east-1:111122223333:table/Customers/export/*`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Orders/export/*` |
| `dynamodb:DescribeGlobalTable` | `*` | `arn:aws:dynamodb::111122223333:global-table/Customers`<br>`arn:aws:dynamodb::111122223333:global-table/Orders` |
| `dynamodb:DescribeGlobalTableSettings` | `*` | `arn:aws:dynamodb::111122223333:global-table/Customers`<br>`arn:aws:dynamodb::111122223333:global-table/Orders` |
| `dynamodb:DescribeKinesisStreamingDestination` | `*` | `arn:aws:dynamodb:us-east-1:111122223333:table/Customers`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Orders` |
| `dynamodb:DescribeStream` | `*` | `arn:aws:dynamodb:us-east-1:111122223333:table/Customers/stream/*`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Orders/stream/*` |
| `dynamodb:DescribeTable` | `*` | `arn:aws:dynamodb:us-east-1:111122223333:table/Customers`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Orders` |
| `dynamodb:DescribeTableReplicaAutoScaling` | `*` | `arn:aws:dynamodb:us-east-1:111122223333:table/Customers`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Orders` |
| `dynamodb:DescribeTimeToLive` | `*` | `arn:aws:dynamodb:us-east-1:111122223333:table/Customers`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Orders` |
| `dynamodb:DisableKinesisStreamingDestination` | `*` | `arn:aws:dynamodb:us-east-1:111122223333:table/Customers`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Orders` |
| `dynamodb:EnableKinesisStreamingDestination` | `*` | `arn:aws:dynamodb:us-east-1:111122223333:table/Customers`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Orders` |
| `dynamodb:ExportTableToPointInTime` | `*` | `arn:aws:dynamodb:us-east-1:111122223333:table/Customers`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Customers/export/*`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Orders`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Orders/export/*` |
| `dynamodb:GetItem` | `*` | `arn:aws:dynamodb:us-east-1:111122223333:table/Customers`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Orders` |
| `dynamodb:GetItm` | `*` | `arn:aws:dynamodb:us-east-1:111122223333:table/Customers`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Orders` |
| `dynamodb:GetRecords` | `*` | `arn:aws:dynamodb:us-east-1:111122223333:table/Customers`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Customers/stream/*`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Orders`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Orders/stream/*` |
| `dynamodb:GetShardIterator` | `*` | `arn:aws:dynamodb:us-east-1:111122223333:table/Customers/stream/*`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Orders/stream/*` |
| `dynamodb:ImportTable` | `*` | `arn:aws:dynamodb:us-east-1:111122223333:table/Customers`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Orders` |
| `dynamodb:ListTagsOfResource` | `*` | `arn:aws:dynamodb:us-east-1:111122223333:table/Customers`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Orders` |
| `dynamodb:PartiQLDelete` | `*` | `arn:aws:dynamodb:us-east-1:111122223333:table/Customers`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Orders` |
| `dynamodb:PartiQLInsert` | `*` | `arn:aws:dynamodb:us-east-1:111122223333:table/Customers`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Orders` |
| `dynamodb:PartiQLSelect` | `*` | `arn:aws:dynamodb:us-east-1:111122223333:table/Customers`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Orders` |
| `dynamodb:PartiQLUpdate` | `*` | `arn:aws:dynamodb:us-east-1:111122223333:table/Customers`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Orders` |
| `dynamodb:PutItem` | `*` | `arn:aws:dynamodb:us-east-1:111122223333:table/Customers`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Orders` |
| `dynamodb:Query` | `*` | `arn:aws:dynamodb:us-east-1:111122223333:table/Customers`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Customers/index/by_email`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Orders`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Orders/index/by_email` |
| `dynamodb:RestoreTableFromAwsBackup` | `*` | `arn:aws:dynamodb:us-east-1:111122223333:table/Customers`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Orders` |
| `dynamodb:RestoreTableFromBackup` | `*` | `arn:aws:dynamodb:us-east-1:111122223333:table/Customers`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Customers/backup/*`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Orders`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Orders/backup/*` |
| `dynamodb:Scan` | `*` | `arn:aws:dynamodb:us-east-1:111122223333:table/Customers`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Customers/index/by_email`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Orders`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Orders/index/by_email` |
| `dynamodb:TagResource` | `*` | `arn:aws:dynamodb:us-east-1:111122223333:table/Customers`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Orders` |
| `dynamodb:UntagResource` | `*` | `arn:aws:dynamodb:us-east-1:111122223333:table/Customers`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Orders` |
| `dynamodb:UpdateContinuousBackups` | `*` | `arn:aws:dynamodb:us-east-1:111122223333:table/Customers`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Orders` |
| `dynamodb:UpdateContributorInsights` | `*` | `arn:aws:dynamodb:us-east-1:111122223333:table/Customers`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Orders` |
| `dynamodb:UpdateGlobalTable` | `*` | `arn:aws:dynamodb::111122223333:global-table/Customers`<br>`arn:aws:dynamodb::111122223333:global-table/Orders` |
| `dynamodb:UpdateGlobalTableSettings` | `*` | `arn:aws:dynamodb::111122223333:global-table/Customers`<br>`arn:aws:dynamodb::111122223333:global-table/Orders` |
| `dynamodb:UpdateItem` | `*` | `arn:aws:dynamodb:us-east-1:111122223333:table/Customers`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Orders` |
| `dynamodb:UpdateKinesisStreamingDestination` | `*` | `arn:aws:dynamodb:us-east-1:111122223333:table/Customers`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Orders` |
| `dynamodb:UpdateTable` | `*` | `arn:aws:dynamodb:us-east-1:111122223333:table/Customers`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Orders` |
| `dynamodb:UpdateTableReplicaAutoScaling` | `*` | `arn:aws:dynamodb:us-east-1:111122223333:table/Customers`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Orders` |
| `dynamodb:UpdateTimeToLive` | `*` | `arn:aws:dynamodb:us-east-1:111122223333:table/Customers`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Orders` |
| `s3:GetObject` | `*` | `arn:aws:dynamodb:us-east-1:111122223333:table/Customers`<br>`arn:aws:dynamodb:us-east-1:111122223333:table/Orders` |

### Removed (action, resource) pairs

| Action | Resource |
|--------|----------|
| `dynamodb:BatchGetItem` | `*` |
| `dynamodb:BatchWriteItem` | `*` |
| `dynamodb:ConditionCheckItem` | `*` |
| `dynamodb:CreateBackup` | `*` |
| `dynamodb:CreateGlobalTable` | `*` |
| `dynamodb:CreateTableReplica` | `*` |
| `dynamodb:DeleteBackup` | `*` |
| `dynamodb:DeleteItem` | `*` |
| `dynamodb:DeleteTable` | `*` |
| `dynamodb:DeleteTableReplica` | `*` |
| `dynamodb:DescribeBackup` | `*` |
| `dynamodb:DescribeContinuousBackups` | `*` |
| `dynamodb:DescribeContributorInsights` | `*` |
| `dynamodb:DescribeExport` | `*` |
| `dynamodb:DescribeGlobalTable` | `*` |
| `dynamodb:DescribeGlobalTableSettings` | `*` |
| `dynamodb:DescribeKinesisStreamingDestination` | `*` |
| `dynamodb:DescribeStream` | `*` |
| `dynamodb:DescribeTable` | `*` |
| `dynamodb:DescribeTableReplicaAutoScaling` | `*` |
| `dynamodb:DescribeTimeToLive` | `*` |
| `dynamodb:DisableKinesisStreamingDestination` | `*` |
| `dynamodb:EnableKinesisStreamingDestination` | `*` |
| `dynamodb:ExportTableToPointInTime` | `*` |
| `dynamodb:GetItem` | `*` |
| `dynamodb:GetItm` | `*` |
| `dynamodb:GetRecords` | `*` |
| `dynamodb:GetShardIterator` | `*` |
| `dynamodb:ImportTable` | `*` |
| `dynamodb:ListTagsOfResource` | `*` |
| `dynamodb:PartiQLDelete` | `*` |
| `dynamodb:PartiQLInsert` | `*` |
| `dynamodb:PartiQLSelect` | `*` |
| `dynamodb:PartiQLUpdate` | `*` |
| `dynamodb:PutItem` | `*` |
| `dynamodb:Query` | `*` |
| `dynamodb:RestoreTableFromAwsBackup` | `*` |
| `dynamodb:RestoreTableFromBackup` | `*` |
| `dynamodb:Scan` | `*` |
| `dynamodb:TagResource` | `*` |
| `dynamodb:UntagResource` | `*` |
| `dynamodb:UpdateContinuousBackups` | `*` |
| `dynamodb:UpdateContributorInsights` | `*` |
| `dynamodb:UpdateGlobalTable` | `*` |
| `dynamodb:UpdateGlobalTableSettings` | `*` |
| `dynamodb:UpdateItem` | `*` |
| `dynamodb:UpdateKinesisStreamingDestination` | `*` |
| `dynamodb:UpdateTable` | `*` |
| `dynamodb:UpdateTableReplicaAutoScaling` | `*` |
| `dynamodb:UpdateTimeToLive` | `*` |
| `s3:GetObject` | `*` |


## 4. Refactored policy JSON

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Sid": "OverlyBroad_Table",
      "Action": [
        "dynamodb:BatchGetItem",
        "dynamodb:BatchWriteItem",
        "dynamodb:ConditionCheckItem",
        "dynamodb:CreateTableReplica",
        "dynamodb:DeleteItem",
        "dynamodb:DeleteTable",
        "dynamodb:DeleteTableReplica",
        "dynamodb:DescribeContinuousBackups",
        "dynamodb:DescribeContributorInsights",
        "dynamodb:DescribeKinesisStreamingDestination",
        "dynamodb:DescribeTable",
        "dynamodb:DescribeTableReplicaAutoScaling",
        "dynamodb:DescribeTimeToLive",
        "dynamodb:DisableKinesisStreamingDestination",
        "dynamodb:EnableKinesisStreamingDestination",
        "dynamodb:ExportTableToPointInTime",
        "dynamodb:GetItem",
        "dynamodb:GetRecords",
        "dynamodb:ImportTable",
        "dynamodb:ListTagsOfResource",
        "dynamodb:PartiQLDelete",
        "dynamodb:PartiQLInsert",
        "dynamodb:PartiQLSelect",
        "dynamodb:PartiQLUpdate",
        "dynamodb:PutItem",
        "dynamodb:Query",
        "dynamodb:RestoreTableFromAwsBackup",
        "dynamodb:RestoreTableFromBackup",
        "dynamodb:Scan",
        "dynamodb:TagResource",
        "dynamodb:UntagResource",
        "dynamodb:UpdateContinuousBackups",
        "dynamodb:UpdateContributorInsights",
        "dynamodb:UpdateItem",
        "dynamodb:UpdateKinesisStreamingDestination",
        "dynamodb:UpdateTable",
        "dynamodb:UpdateTableReplicaAutoScaling",
        "dynamodb:UpdateTimeToLive"
      ],
      "Resource": [
        "arn:aws:dynamodb:us-east-1:111122223333:table/Orders",
        "arn:aws:dynamodb:us-east-1:111122223333:table/Customers"
      ]
    },
    {
      "Effect": "Allow",
      "Sid": "OverlyBroad_Index",
      "Action": [
        "dynamodb:Query",
        "dynamodb:Scan"
      ],
      "Resource": [
        "arn:aws:dynamodb:us-east-1:111122223333:table/Orders/index/by_email",
        "arn:aws:dynamodb:us-east-1:111122223333:table/Customers/index/by_email"
      ]
    },
    {
      "Effect": "Allow",
      "Sid": "OverlyBroad_Stream",
      "Action": [
        "dynamodb:DescribeStream",
        "dynamodb:GetRecords",
        "dynamodb:GetShardIterator"
      ],
      "Resource": [
        "arn:aws:dynamodb:us-east-1:111122223333:table/Orders/stream/*",
        "arn:aws:dynamodb:us-east-1:111122223333:table/Customers/stream/*"
      ]
    },
    {
      "Effect": "Allow",
      "Sid": "OverlyBroad_Backup",
      "Action": [
        "dynamodb:CreateBackup",
        "dynamodb:DeleteBackup",
        "dynamodb:DescribeBackup",
        "dynamodb:RestoreTableFromBackup"
      ],
      "Resource": [
        "arn:aws:dynamodb:us-east-1:111122223333:table/Orders/backup/*",
        "arn:aws:dynamodb:us-east-1:111122223333:table/Customers/backup/*"
      ]
    },
    {
      "Effect": "Allow",
      "Sid": "OverlyBroad_Export",
      "Action": [
        "dynamodb:DescribeExport",
        "dynamodb:ExportTableToPointInTime"
      ],
      "Resource": [
        "arn:aws:dynamodb:us-east-1:111122223333:table/Orders/export/*",
        "arn:aws:dynamodb:us-east-1:111122223333:table/Customers/export/*"
      ]
    },
    {
      "Effect": "Allow",
      "Sid": "OverlyBroad_GlobalTable",
      "Action": [
        "dynamodb:CreateGlobalTable",
        "dynamodb:DescribeGlobalTable",
        "dynamodb:DescribeGlobalTableSettings",
        "dynamodb:UpdateGlobalTable",
        "dynamodb:UpdateGlobalTableSettings"
      ],
      "Resource": [
        "arn:aws:dynamodb::111122223333:global-table/Orders",
        "arn:aws:dynamodb::111122223333:global-table/Customers"
      ]
    },
    {
      "Effect": "Allow",
      "Sid": "OverlyBroad_ServiceLevel",
      "Action": [
        "dynamodb:CreateTable",
        "dynamodb:DescribeEndpoints",
        "dynamodb:DescribeLimits",
        "dynamodb:DescribeReservedCapacity",
        "dynamodb:DescribeReservedCapacityOfferings",
        "dynamodb:ListBackups",
        "dynamodb:ListContributorInsights",
        "dynamodb:ListExports",
        "dynamodb:ListGlobalTables",
        "dynamodb:ListImports",
        "dynamodb:ListStreams",
        "dynamodb:ListTables",
        "dynamodb:PurchaseReservedCapacityOfferings"
      ],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Sid": "MixedWithS3_Table",
      "Action": [
        "dynamodb:GetItem",
        "dynamodb:Scan",
        "s3:GetObject"
      ],
      "Resource": [
        "arn:aws:dynamodb:us-east-1:111122223333:table/Orders",
        "arn:aws:dynamodb:us-east-1:111122223333:table/Customers"
      ]
    },
    {
      "Effect": "Allow",
      "Sid": "MixedWithS3_Index",
      "Action": [
        "dynamodb:Scan"
      ],
      "Resource": [
        "arn:aws:dynamodb:us-east-1:111122223333:table/Orders/index/by_email",
        "arn:aws:dynamodb:us-east-1:111122223333:table/Customers/index/by_email"
      ]
    },
    {
      "Effect": "Allow",
      "Sid": "ServiceLevel_ServiceLevel",
      "Action": [
        "dynamodb:DescribeLimits",
        "dynamodb:ListTables"
      ],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Sid": "TypoAction_Table",
      "Action": [
        "dynamodb:GetItm"
      ],
      "Resource": [
        "arn:aws:dynamodb:us-east-1:111122223333:table/Orders",
        "arn:aws:dynamodb:us-east-1:111122223333:table/Customers"
      ]
    }
  ]
}
```

## 5. Recommendations

- Prefer explicit `Action` lists over `dynamodb:*` or verb-level wildcards.
- Bind every table-scoped action to a specific `arn:aws:dynamodb:...:table/NAME` ARN.
- Scope index, stream, and backup ARNs off the parent table.
- Put service-level list/describe actions in their own statement with `Resource: "*"`.
- Add `Condition` blocks (e.g. `aws:SourceIp`, `aws:MultiFactorAuthPresent`) on destructive actions.
- Re-run Access Analyzer validation in CI on every policy change.