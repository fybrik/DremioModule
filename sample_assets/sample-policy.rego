package dataapi.authz

rule[{"action": {"name":"RemoveAction", "columns": column_names}, "policy": description}] {
  description := "Remove columns tagged as PII in datasets tagged with finance = true"
  input.action.actionType == "read"
  input.resource.metadata.tags.finance
  column_names := [input.resource.metadata.columns[i].name | input.resource.metadata.columns[i].tags.PII]
  count(column_names) > 0
}
