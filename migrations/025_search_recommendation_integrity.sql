ALTER TABLE charm_control.experiment_search_recommendations
    DROP CONSTRAINT IF EXISTS experiment_search_method_stage_consistent,
    DROP CONSTRAINT IF EXISTS experiment_search_candidate_consistent,
    DROP CONSTRAINT IF EXISTS experiment_search_acquisition_consistent,
    DROP CONSTRAINT IF EXISTS experiment_search_acquisition_value_finite,
    DROP CONSTRAINT IF EXISTS experiment_search_probability_valid;

ALTER TABLE charm_control.experiment_search_recommendations
    ADD CONSTRAINT experiment_search_method_stage_consistent CHECK (
        (method = 'postgresql_default' AND stage = 'DEFAULT')
        OR (method = 'random' AND stage = 'RANDOM')
        OR (method = 'sobol' AND stage = 'SOBOL')
        OR (method = 'standard_bo' AND stage IN ('BO_INITIALIZATION','STANDARD_BO'))
        OR (method = 'constrained_bo' AND stage IN ('BO_INITIALIZATION','CONSTRAINED_BO'))
    ),
    ADD CONSTRAINT experiment_search_candidate_consistent CHECK (
        (method = 'postgresql_default' AND input_vector IS NULL AND configuration = '{}'::jsonb)
        OR (method <> 'postgresql_default' AND input_vector IS NOT NULL)
    ),
    ADD CONSTRAINT experiment_search_acquisition_consistent CHECK (
        (
            stage IN ('DEFAULT','RANDOM','SOBOL','BO_INITIALIZATION')
            AND acquisition_name IS NULL
            AND acquisition_value IS NULL
            AND probability_feasible IS NULL
        )
        OR (
            stage = 'STANDARD_BO'
            AND acquisition_name = 'qLogNEHVI'
            AND acquisition_value IS NOT NULL
            AND probability_feasible IS NOT NULL
        )
        OR (
            stage = 'CONSTRAINED_BO'
            AND acquisition_name = 'qLogNEHVI_CONSTRAINED'
            AND acquisition_value IS NOT NULL
            AND probability_feasible IS NOT NULL
        )
    ),
    ADD CONSTRAINT experiment_search_acquisition_value_finite CHECK (
        acquisition_value IS NULL
        OR (
            acquisition_value > '-Infinity'::double precision
            AND acquisition_value < 'Infinity'::double precision
        )
    ),
    ADD CONSTRAINT experiment_search_probability_valid CHECK (
        probability_feasible IS NULL
        OR probability_feasible BETWEEN 0 AND 1
    );

CREATE OR REPLACE FUNCTION charm_control.prevent_search_recommendation_rewrite()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.arm_id IS DISTINCT FROM OLD.arm_id
       OR NEW.budget_position IS DISTINCT FROM OLD.budget_position
       OR NEW.method IS DISTINCT FROM OLD.method
       OR NEW.stage IS DISTINCT FROM OLD.stage
       OR NEW.random_seed IS DISTINCT FROM OLD.random_seed
       OR NEW.input_vector IS DISTINCT FROM OLD.input_vector
       OR NEW.configuration IS DISTINCT FROM OLD.configuration
       OR NEW.training_observation_ids IS DISTINCT FROM OLD.training_observation_ids
       OR NEW.acquisition_name IS DISTINCT FROM OLD.acquisition_name
       OR NEW.acquisition_value IS DISTINCT FROM OLD.acquisition_value
       OR NEW.probability_feasible IS DISTINCT FROM OLD.probability_feasible
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'experiment search recommendation evidence is immutable';
    END IF;
    IF OLD.trial_id IS NOT NULL AND NEW.trial_id IS DISTINCT FROM OLD.trial_id THEN
        RAISE EXCEPTION 'experiment search recommendation trial link is immutable once set';
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS experiment_search_recommendation_immutable
    ON charm_control.experiment_search_recommendations;

CREATE TRIGGER experiment_search_recommendation_immutable
BEFORE UPDATE ON charm_control.experiment_search_recommendations
FOR EACH ROW
EXECUTE FUNCTION charm_control.prevent_search_recommendation_rewrite();
