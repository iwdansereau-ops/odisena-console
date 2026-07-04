# DynamoDB IAM Policy Refactor Report

- Worst risk in original policy: **CRITICAL**
- Validation backend used: **offline**
- Overall validation OK: **True**
- Statements: original=2, refactored=2

## 1. Security findings on the ORIGINAL policy

| Severity | Code | Stmt | Sid | Message |
|----------|------|------|-----|---------|
| LOW | `NO_CONDITION_ON_DESTRUCTIVE` | 1 | CrossAccountWrites | Destructive actions allowed without any Condition (e.g., aws:SourceIp, aws:MultiFactorAuthPresent). Consider hardening. |
| CRITICAL | `PUBLIC_PRINCIPAL` | 0 | PublicRead | Resource policy grants access to Principal "*" without any aws:PrincipalOrgID / aws:PrincipalArn / aws:SourceAccount condition — this is effectively public. |

## 2. Access Analyzer / structural validation on the REFACTORED policy

| Source | Severity | Code | Message |
|--------|----------|------|---------|
| offline | SUGGESTION | `NO_CONDITION_ON_DESTRUCTIVE` | Destructive actions allowed without any Condition (e.g., aws:SourceIp, aws:MultiFactorAuthPresent). Consider hardening. |

## 3. Action → Resource matrix for the REFACTORED policy

Every allowed DynamoDB action and the exact ARNs it is limited to.

| Sid | Effect | Class | Action | Conditioned | Resources |
|-----|--------|-------|--------|-------------|-----------|
| PublicRead_Table | Allow | read | `dynamodb:GetItem` | no | `arn:aws:dynamodb:us-east-1:111122223333:table/Orders` |
| CrossAccountWrites_Table | Allow | destructive | `dynamodb:BatchWriteItem` | no | `arn:aws:dynamodb:us-east-1:111122223333:table/Orders` |
| CrossAccountWrites_Table | Allow | write/admin | `dynamodb:PutItem` | no | `arn:aws:dynamodb:us-east-1:111122223333:table/Orders` |

## Diff: original vs refactored

- Actions in original: **3**, in refactored: **3**
- Actions narrowed off `"*"`: **0**
- (action, resource) pairs removed: **0**
- (action, resource) pairs added: **0**


## 4. Refactored policy JSON

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Sid": "PublicRead_Table",
      "Action": [
        "dynamodb:GetItem"
      ],
      "Resource": "arn:aws:dynamodb:us-east-1:111122223333:table/Orders"
    },
    {
      "Effect": "Allow",
      "Sid": "CrossAccountWrites_Table",
      "Action": [
        "dynamodb:BatchWriteItem",
        "dynamodb:PutItem"
      ],
      "Resource": "arn:aws:dynamodb:us-east-1:111122223333:table/Orders"
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