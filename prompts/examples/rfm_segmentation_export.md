# Worked example: RFM export: warehouse request bodies and the HubSpot property

Reference for the export plan in `prompts/jobs/sql/rfm_segmentation.md`. Read it when the user wants the request bodies
built in the warehouse rather than with `lcm sync`, or needs the segment property created in HubSpot.

## Request bodies built in the warehouse

After materializing the scores (`CREATE TABLE lmm_rfm_scores AS <scoring query>`):

```sql postgres
-- dialect: postgresql
-- no-run: reads the materialized scoring output
SELECT hubspot_batch,
       json_build_object('inputs', json_agg(hubspot_input ORDER BY user_id)) AS request_body
FROM lmm_rfm_scores
GROUP BY hubspot_batch
ORDER BY hubspot_batch;
```

```sql postgres
-- dialect: postgresql
-- no-run: reads the materialized scoring output
SELECT klaviyo_job,
       json_build_object('data', json_build_object(
         'type', 'profile-bulk-import-job',
         'attributes', json_build_object('profiles', json_build_object('data', json_agg(klaviyo_profile ORDER BY user_id)))
       )) AS request_body
FROM lmm_rfm_scores
GROUP BY klaviyo_job
ORDER BY klaviyo_job;
```

## The HubSpot property for the segment

POST to the contact properties endpoint for your API version:

```json
{
  "name": "lmm_rfm_segment",
  "label": "RFM segment",
  "type": "enumeration",
  "fieldType": "select",
  "groupName": "contactinformation",
  "description": "Set by the LMM Engine RFM job; scored on lmm_rfm_scored_on",
  "options": [
    {"label": "Instant churn", "value": "instant_churn", "displayOrder": 1},
    {"label": "Champions", "value": "champions", "displayOrder": 2},
    {"label": "Loyal", "value": "loyal", "displayOrder": 3},
    {"label": "Cannot lose", "value": "cannot_lose", "displayOrder": 4},
    {"label": "At risk", "value": "at_risk", "displayOrder": 5},
    {"label": "New customers", "value": "new_customers", "displayOrder": 6},
    {"label": "Potential loyalists", "value": "potential_loyalists", "displayOrder": 7},
    {"label": "Promising", "value": "promising", "displayOrder": 8},
    {"label": "Need attention", "value": "need_attention", "displayOrder": 9},
    {"label": "About to sleep", "value": "about_to_sleep", "displayOrder": 10},
    {"label": "Hibernating", "value": "hibernating", "displayOrder": 11}
  ]
}
```
