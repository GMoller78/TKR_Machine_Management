-- migration_add_operator_to_checklist_sqlite.sql

-- SQLite typically runs DDL statements in their own implicit transaction,
-- but using BEGIN/COMMIT is good practice if running multiple steps manually.
BEGIN TRANSACTION;

-- Step 1: Add the new 'operator' column with NOT NULL and a DEFAULT value.
-- SQLite will automatically populate existing rows with this default value ('Unknown Operator').
-- Adjust VARCHAR(100) if needed, though SQLite type affinity means it's mostly advisory.
ALTER TABLE checklist
ADD COLUMN operator VARCHAR(100) NOT NULL DEFAULT 'Unknown Operator';

-- Note: If you need to enforce a constraint *without* a default later,
-- or change constraints, you would typically need to follow the more
-- complex SQLite procedure:
-- 1. CREATE new table with the final schema
-- 2. INSERT data from old table into new table (SELECT *, 'some_value' FROM old_table)
-- 3. DROP old table
-- 4. RENAME new table to old table name
-- 5. Recreate indexes/triggers if any.
-- However, for adding a mandatory column with a sensible default, the ADD COLUMN ... DEFAULT method is sufficient and much simpler.

COMMIT;

-- Informative message (optional - won't show in most tools)
-- SELECT 'Successfully added mandatory operator column with default value to the checklist table (SQLite).' AS message;