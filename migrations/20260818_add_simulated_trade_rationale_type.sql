SET @rationale_type_column_exists = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'simulated_trades'
      AND COLUMN_NAME = 'rationale_label_type'
);

SET @add_rationale_type_column = IF(
    @rationale_type_column_exists = 0,
    'ALTER TABLE simulated_trades ADD COLUMN rationale_label_type VARCHAR(40) NOT NULL DEFAULT ''UNCLASSIFIED'' AFTER decision_reason',
    'SELECT 1'
);

PREPARE rationale_type_column_statement FROM @add_rationale_type_column;
EXECUTE rationale_type_column_statement;
DEALLOCATE PREPARE rationale_type_column_statement;
