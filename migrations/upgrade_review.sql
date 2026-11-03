BEGIN;

-- Running upgrade 0007 -> 0008

CREATE TABLE external_api_responses (
    id UUID NOT NULL, 
    run_id UUID NOT NULL, 
    source VARCHAR(80) NOT NULL, 
    operation VARCHAR(120) NOT NULL, 
    collected_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    payload JSONB NOT NULL, 
    CONSTRAINT pk_external_api_responses PRIMARY KEY (id), 
    CONSTRAINT fk_external_api_responses_run_id_collection_runs FOREIGN KEY(run_id) REFERENCES collection_runs (id)
);

CREATE INDEX ix_external_api_responses_source_collected ON external_api_responses (source, collected_at);

CREATE INDEX ix_external_api_responses_run_id ON external_api_responses (run_id);

CREATE TABLE external_api_observations (
    id UUID NOT NULL, 
    response_id UUID NOT NULL, 
    record_key VARCHAR(200) NOT NULL, 
    name VARCHAR(300), 
    observed_at TIMESTAMP WITH TIME ZONE, 
    latitude FLOAT, 
    longitude FLOAT, 
    data JSONB NOT NULL, 
    CONSTRAINT pk_external_api_observations PRIMARY KEY (id), 
    CONSTRAINT uq_external_api_observation_record UNIQUE (response_id, record_key), 
    CONSTRAINT ck_external_api_observations_ck_api_observation_latitude CHECK (latitude BETWEEN -90 AND 90), 
    CONSTRAINT ck_external_api_observations_ck_api_observation_longitude CHECK (longitude BETWEEN -180 AND 180), 
    CONSTRAINT ck_external_api_observations_ck_api_observation_coordinate_pair CHECK ((latitude IS NULL) = (longitude IS NULL)), 
    CONSTRAINT fk_external_api_observations_response_id_external_api_responses FOREIGN KEY(response_id) REFERENCES external_api_responses (id)
);

CREATE INDEX ix_external_api_observations_record_time ON external_api_observations (record_key, observed_at);

CREATE INDEX ix_collection_runs_source_started ON collection_runs (source, started_at);

CREATE INDEX ix_collection_runs_started ON collection_runs (started_at);

UPDATE alembic_version SET version_num='0008' WHERE alembic_version.version_num = '0007';

COMMIT;

