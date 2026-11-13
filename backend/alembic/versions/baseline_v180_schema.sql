-- Star schema as produced by the released v180 migration chain (revisions 20260722_0001 .. 20260903_0105),
-- captured with pg_dump --schema-only on 4 September 2026 and applied verbatim by 20260904_0001_baseline.
--
-- PostgreSQL database dump
--


-- Dumped from database version 18.4 (Debian 18.4-1.pgdg13+1)
-- Dumped by pg_dump version 18.4 (Debian 18.4-1.pgdg13+1)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: vector; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;


--
-- Name: EXTENSION vector; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION vector IS 'vector data type and ivfflat and hnsw access methods';


--
-- Name: star_emit_system_change(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.star_emit_system_change() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE
            row_data jsonb;
            argument_index integer;
            event_name text;
            payload jsonb;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                row_data := to_jsonb(OLD);
            ELSE
                row_data := to_jsonb(NEW);
            END IF;
            FOR argument_index IN 0..TG_NARGS - 1 LOOP
                event_name := TG_ARGV[argument_index];
                payload := jsonb_strip_nulls(jsonb_build_object(
                    'event', event_name,
                    'table', TG_TABLE_NAME,
                    'entity_id', row_data ->> 'id',
                    'asset_id', COALESCE(
                        row_data ->> 'asset_id',
                        CASE WHEN TG_TABLE_NAME = 'assets' THEN row_data ->> 'id' END
                    ),
                    'connection_id', COALESCE(
                        row_data ->> 'connection_id',
                        CASE WHEN TG_TABLE_NAME = 'immich_connections' THEN row_data ->> 'id' END
                    ),
                    'batch_id', COALESCE(
                        row_data ->> 'batch_id',
                        CASE WHEN TG_TABLE_NAME = 'writeback_batches' THEN row_data ->> 'id' END
                    ),
                    'group_id', COALESCE(
                        row_data ->> 'group_id',
                        CASE WHEN TG_TABLE_NAME = 'collection_groups' THEN row_data ->> 'id' END
                    )
                ));
                PERFORM pg_notify('star_system_changes', payload::text);
            END LOOP;
            IF TG_OP = 'DELETE' THEN
                RETURN OLD;
            END IF;
            RETURN NEW;
        END;
        $$;


--
-- Name: propagate_group_member_evidence(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.propagate_group_member_evidence() RETURNS trigger
    LANGUAGE plpgsql
    AS $$ BEGIN UPDATE collection_group_members SET membership_confidence=NEW.confidence, evidence=NEW.source_facts WHERE group_id=NEW.id; RETURN NEW; END; $$;


--
-- Name: sync_group_member_evidence(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.sync_group_member_evidence() RETURNS trigger
    LANGUAGE plpgsql
    AS $$ BEGIN SELECT confidence, source_facts INTO NEW.membership_confidence, NEW.evidence FROM collection_groups WHERE id=NEW.group_id; RETURN NEW; END; $$;


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: album_asset_references; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.album_asset_references (
    id uuid NOT NULL,
    album_id uuid NOT NULL,
    asset_id uuid NOT NULL,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: album_references; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.album_references (
    id uuid NOT NULL,
    connection_id uuid NOT NULL,
    immich_album_id character varying(256) NOT NULL,
    name character varying(1024) NOT NULL,
    access_state character varying(32) NOT NULL,
    source_data jsonb NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);




--
-- Name: analysis_generations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.analysis_generations (
    id uuid NOT NULL,
    asset_id uuid NOT NULL,
    asset_revision_id uuid NOT NULL,
    parent_generation_id uuid,
    sequence integer NOT NULL,
    scope jsonb NOT NULL,
    start_stage character varying(64) NOT NULL,
    expanded_stages jsonb NOT NULL,
    reuse_policy character varying(32) NOT NULL,
    publication_policy character varying(32) NOT NULL,
    model_policy_version character varying(128) NOT NULL,
    inference_profiles jsonb NOT NULL,
    prompt_version character varying(128) NOT NULL,
    schema_version character varying(128) NOT NULL,
    calibration_version character varying(128) NOT NULL,
    state character varying(32) NOT NULL,
    reason character varying(128) NOT NULL,
    created_by uuid,
    published_at timestamp with time zone,
    completed_at timestamp with time zone,
    superseded_at timestamp with time zone,
    failure_class character varying(128),
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    application_release character varying(32) NOT NULL,
    personal_ranker_version_id uuid,
    personal_ranker_version integer,
    prompt_revisions jsonb NOT NULL
);


--
-- Name: analysis_nodes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.analysis_nodes (
    id uuid NOT NULL,
    generation_id uuid NOT NULL,
    asset_id uuid NOT NULL,
    stage_key character varying(64) NOT NULL,
    state character varying(32) NOT NULL,
    required boolean NOT NULL,
    applicable boolean NOT NULL,
    analyzer_version character varying(128) NOT NULL,
    model_key character varying(512),
    inference_profile character varying(128) NOT NULL,
    input_digest character varying(64) NOT NULL,
    attempt_count integer NOT NULL,
    lease_generation integer NOT NULL,
    worker_runtime_ms bigint NOT NULL,
    failure_class character varying(128),
    task_id uuid,
    result_analysis_run_id uuid,
    superseded_by_node_id uuid,
    started_at timestamp with time zone,
    completed_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    output_digest character varying(64),
    decision jsonb DEFAULT '{}'::jsonb NOT NULL
);


--
-- Name: analysis_obligations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.analysis_obligations (
    id uuid NOT NULL,
    library_program_id uuid,
    processing_batch_id uuid,
    asset_id uuid NOT NULL,
    asset_revision_id uuid NOT NULL,
    task_id uuid,
    stage_key character varying(64) NOT NULL,
    model_family_key character varying(128) NOT NULL,
    model_key character varying(512) NOT NULL,
    processor_version character varying(128) NOT NULL,
    completion_role character varying(32) DEFAULT 'enrichment'::character varying NOT NULL,
    state character varying(32) DEFAULT 'planned'::character varying NOT NULL,
    priority_tier integer DEFAULT 5 NOT NULL,
    priority_score integer DEFAULT 0 NOT NULL,
    priority_reasons jsonb DEFAULT '[]'::json NOT NULL,
    evidence_digest character varying(64),
    attempt_count integer DEFAULT 0 NOT NULL,
    last_error_class character varying(128),
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    review_cycle_id uuid,
    CONSTRAINT ck_analysis_obligations_valid_analysis_obligation_prior_5cd9 CHECK (((priority_score >= 0) AND (priority_score <= 10000))),
    CONSTRAINT ck_analysis_obligations_valid_analysis_obligation_priority_tier CHECK (((priority_tier >= 0) AND (priority_tier <= 5)))
);


--
-- Name: analysis_publication_snapshots; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.analysis_publication_snapshots (
    id uuid NOT NULL,
    asset_id uuid NOT NULL,
    asset_revision_id uuid NOT NULL,
    analysis_generation_id uuid NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: analysis_runs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.analysis_runs (
    id uuid NOT NULL,
    campaign_id uuid,
    library_program_id uuid,
    asset_id uuid NOT NULL,
    asset_revision_id uuid NOT NULL,
    analysis_type character varying(128) NOT NULL,
    processor_version character varying(128) NOT NULL,
    model_key character varying(512) NOT NULL,
    model_version_id uuid,
    status character varying(32) NOT NULL,
    limitations jsonb NOT NULL,
    result_digest character varying(64),
    error_class character varying(128),
    started_at timestamp with time zone NOT NULL,
    completed_at timestamp with time zone,
    worker_runtime_ms bigint DEFAULT '0'::bigint NOT NULL,
    analysis_generation_id uuid,
    input_provenance jsonb DEFAULT '{}'::jsonb NOT NULL,
    result_payload jsonb
)
WITH (autovacuum_vacuum_scale_factor='0.05', autovacuum_vacuum_threshold='100', autovacuum_analyze_scale_factor='0.02', autovacuum_analyze_threshold='50');


--
-- Name: analysis_saved_filters; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.analysis_saved_filters (
    id uuid NOT NULL,
    owner_id uuid NOT NULL,
    name character varying(128) NOT NULL,
    filter_definition jsonb NOT NULL,
    asset_ids jsonb NOT NULL,
    active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: api_credentials; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.api_credentials (
    id uuid NOT NULL,
    connection_id uuid NOT NULL,
    kind character varying(9) NOT NULL,
    encrypted_secret bytea NOT NULL,
    encryption_key_id character varying(128) NOT NULL,
    permission_snapshot jsonb NOT NULL,
    rotated_at timestamp with time zone,
    disabled_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: apple_vision_evidence; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.apple_vision_evidence (
    id uuid NOT NULL,
    asset_id uuid NOT NULL,
    asset_revision_id uuid NOT NULL,
    analysis_run_id uuid NOT NULL,
    processor_version character varying(128) NOT NULL,
    aesthetics_score double precision,
    is_utility boolean,
    classifications jsonb DEFAULT '[]'::jsonb NOT NULL,
    features jsonb DEFAULT '{}'::jsonb NOT NULL,
    input_digest character varying(64) NOT NULL,
    worker_runtime_ms bigint DEFAULT '0'::bigint NOT NULL,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: asset_curation_snapshots; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.asset_curation_snapshots (
    asset_id uuid NOT NULL,
    effective_rating integer,
    rating_source character varying(64) DEFAULT 'unrated'::character varying NOT NULL,
    rank_score integer,
    review_priority integer DEFAULT 5000 NOT NULL,
    confidence double precision DEFAULT '0'::double precision NOT NULL,
    agreement double precision DEFAULT '0'::double precision NOT NULL,
    vote_count integer DEFAULT 0 NOT NULL,
    human_priority_points integer DEFAULT 0 NOT NULL,
    dimensions jsonb NOT NULL,
    description text,
    recovery_outlook character varying(32) DEFAULT 'not_assessed'::character varying NOT NULL,
    recovery_actions jsonb NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    personal_score integer,
    personal_confidence double precision DEFAULT '0'::double precision NOT NULL,
    personal_model_version_id uuid,
    asset_revision_id uuid,
    evidence_digest character varying(64),
    analysis_complete boolean DEFAULT false NOT NULL,
    completed_checks jsonb DEFAULT '[]'::jsonb NOT NULL,
    final_summary text,
    assessment_version character varying(64) DEFAULT 'asset-assessment-v3'::character varying NOT NULL,
    student_score integer,
    student_confidence double precision DEFAULT '0'::double precision NOT NULL,
    student_model_version_id uuid,
    destination_recommendations jsonb DEFAULT '{}'::jsonb NOT NULL,
    evidence_plan jsonb DEFAULT '[]'::jsonb NOT NULL,
    content_tags jsonb DEFAULT '[]'::json NOT NULL,
    automated_rating_stage character varying(32) DEFAULT 'unrated'::character varying NOT NULL,
    pipeline_priority_tier integer DEFAULT 5 NOT NULL,
    pipeline_priority_score integer DEFAULT 0 NOT NULL,
    pipeline_priority_reasons jsonb DEFAULT '[]'::json NOT NULL,
    enrichment_coverage jsonb DEFAULT '{}'::json NOT NULL,
    review_contract_version character varying(128) DEFAULT 'legacy'::character varying NOT NULL,
    review_cycle_state character varying(32) DEFAULT 'legacy'::character varying NOT NULL,
    library_points integer,
    library_position integer,
    previous_library_points integer,
    previous_library_position integer,
    preliminary_rating integer,
    ranking_score double precision,
    ranking_confidence double precision NOT NULL,
    ranking_state character varying(32) NOT NULL,
    ranking_version integer NOT NULL,
    ranking_evidence jsonb NOT NULL,
    ranked_at timestamp with time zone,
    sensitive_content character varying(32) DEFAULT 'none'::character varying NOT NULL,
    private_tags jsonb DEFAULT '[]'::jsonb NOT NULL,
    CONSTRAINT ck_asset_curation_snapshots_valid_effective_rating CHECK (((effective_rating IS NULL) OR ((effective_rating >= 1) AND (effective_rating <= 5)))),
    CONSTRAINT ck_asset_curation_snapshots_valid_human_priority_points CHECK (((human_priority_points >= 0) AND (human_priority_points <= 1000))),
    CONSTRAINT ck_asset_curation_snapshots_valid_library_points CHECK (((library_points IS NULL) OR ((library_points >= 1) AND (library_points <= 999999)))),
    CONSTRAINT ck_asset_curation_snapshots_valid_library_position CHECK (((library_position IS NULL) OR (library_position >= 1))),
    CONSTRAINT ck_asset_curation_snapshots_valid_personal_score CHECK (((personal_score IS NULL) OR ((personal_score >= 0) AND (personal_score <= 10000)))),
    CONSTRAINT ck_asset_curation_snapshots_valid_preliminary_rating CHECK (((preliminary_rating IS NULL) OR ((preliminary_rating >= 1) AND (preliminary_rating <= 5)))),
    CONSTRAINT ck_asset_curation_snapshots_valid_rank_score CHECK (((rank_score IS NULL) OR ((rank_score >= 0) AND (rank_score <= 9999)))),
    CONSTRAINT ck_asset_curation_snapshots_valid_ranking_confidence CHECK (((ranking_confidence >= (0)::double precision) AND (ranking_confidence <= (1)::double precision))),
    CONSTRAINT ck_asset_curation_snapshots_valid_review_priority CHECK (((review_priority >= 0) AND (review_priority <= 10000))),
    CONSTRAINT ck_asset_curation_snapshots_valid_student_score CHECK (((student_score IS NULL) OR ((student_score >= 0) AND (student_score <= 9999))))
);


--
-- Name: asset_face_references; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.asset_face_references (
    id uuid NOT NULL,
    connection_id uuid NOT NULL,
    asset_id uuid,
    immich_face_id character varying(256) NOT NULL,
    immich_person_id character varying(256),
    is_visible boolean NOT NULL,
    access_state character varying(32) NOT NULL,
    bounding_box jsonb NOT NULL,
    source_data jsonb NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: asset_immich_contexts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.asset_immich_contexts (
    asset_id uuid NOT NULL,
    asset_info jsonb NOT NULL,
    custom_metadata jsonb NOT NULL,
    ocr jsonb NOT NULL,
    faces jsonb NOT NULL,
    fetch_errors jsonb NOT NULL,
    source_server_version character varying(64),
    fetched_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: asset_metrics; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.asset_metrics (
    id uuid NOT NULL,
    analysis_run_id uuid NOT NULL,
    name character varying(128) NOT NULL,
    value double precision NOT NULL,
    uncertainty double precision NOT NULL,
    explanation text NOT NULL,
    details jsonb NOT NULL
);


--
-- Name: asset_ml_scores; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.asset_ml_scores (
    asset_id uuid NOT NULL,
    personal_score integer,
    personal_confidence double precision DEFAULT 0.0 NOT NULL,
    personal_model_version_id uuid,
    student_score integer,
    student_confidence double precision DEFAULT 0.0 NOT NULL,
    student_model_version_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT asset_ml_scores_personal_score_check CHECK (((personal_score IS NULL) OR ((personal_score >= 0) AND (personal_score <= 10000)))),
    CONSTRAINT asset_ml_scores_student_score_check CHECK (((student_score IS NULL) OR ((student_score >= 0) AND (student_score <= 9999))))
);


--
-- Name: asset_pipeline_state; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.asset_pipeline_state (
    asset_id uuid NOT NULL,
    asset_revision_id uuid,
    evidence_digest character varying(64),
    automated_rating_stage character varying(32) DEFAULT 'unrated'::character varying NOT NULL,
    pipeline_priority_tier integer DEFAULT 5 NOT NULL,
    pipeline_priority_score integer DEFAULT 0 NOT NULL,
    pipeline_priority_reasons jsonb DEFAULT '[]'::jsonb NOT NULL,
    enrichment_coverage jsonb DEFAULT '{}'::jsonb NOT NULL,
    evidence_plan jsonb DEFAULT '[]'::jsonb NOT NULL,
    analysis_complete boolean DEFAULT false NOT NULL,
    completed_checks jsonb DEFAULT '[]'::jsonb NOT NULL,
    review_contract_version character varying(128) DEFAULT 'legacy'::character varying NOT NULL,
    review_cycle_state character varying(32) DEFAULT 'legacy'::character varying NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: asset_ratings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.asset_ratings (
    asset_id uuid NOT NULL,
    effective_rating integer,
    rating_source character varying(64) DEFAULT 'unrated'::character varying NOT NULL,
    rank_score integer,
    review_priority integer DEFAULT 5000 NOT NULL,
    confidence double precision DEFAULT 0.0 NOT NULL,
    agreement double precision DEFAULT 0.0 NOT NULL,
    vote_count integer DEFAULT 0 NOT NULL,
    human_priority_points integer DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT asset_ratings_effective_rating_check CHECK (((effective_rating IS NULL) OR ((effective_rating >= 1) AND (effective_rating <= 5)))),
    CONSTRAINT asset_ratings_human_priority_points_check CHECK (((human_priority_points >= 0) AND (human_priority_points <= 1000))),
    CONSTRAINT asset_ratings_rank_score_check CHECK (((rank_score IS NULL) OR ((rank_score >= 0) AND (rank_score <= 9999)))),
    CONSTRAINT asset_ratings_review_priority_check CHECK (((review_priority >= 0) AND (review_priority <= 10000)))
);


--
-- Name: asset_review_cycles; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.asset_review_cycles (
    id uuid NOT NULL,
    library_program_id uuid,
    processing_batch_id uuid,
    asset_id uuid NOT NULL,
    asset_revision_id uuid NOT NULL,
    policy_version character varying(128) NOT NULL,
    policy_revision integer,
    state character varying(32) DEFAULT 'planned'::character varying NOT NULL,
    gate_state character varying(64) DEFAULT 'waiting'::character varying NOT NULL,
    gate_reasons jsonb DEFAULT '[]'::json NOT NULL,
    qwen_final_required boolean DEFAULT false NOT NULL,
    final_automated_rating integer,
    final_rating_source character varying(64),
    completed_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    analysis_generation_id uuid,
    CONSTRAINT ck_asset_review_cycles_valid_review_cycle_final_rating CHECK (((final_automated_rating IS NULL) OR ((final_automated_rating >= 1) AND (final_automated_rating <= 5))))
);


--
-- Name: asset_revisions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.asset_revisions (
    id uuid NOT NULL,
    asset_id uuid NOT NULL,
    immich_updated_at timestamp with time zone NOT NULL,
    metadata_digest character varying(64) NOT NULL,
    normalized_metadata jsonb NOT NULL,
    file_size bigint,
    width integer,
    height integer,
    duration_seconds double precision,
    rating integer,
    is_favorite boolean NOT NULL,
    is_archived boolean NOT NULL,
    is_trashed boolean NOT NULL,
    observed_at timestamp with time zone NOT NULL,
    CONSTRAINT ck_asset_revisions_valid_rating CHECK (((rating IS NULL) OR ((rating >= 1) AND (rating <= 5))))
);


--
-- Name: asset_search_embeddings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.asset_search_embeddings (
    id uuid NOT NULL,
    asset_id uuid NOT NULL,
    asset_revision_id uuid NOT NULL,
    analysis_run_id uuid NOT NULL,
    analysis_generation_id uuid,
    purpose character varying(64) NOT NULL,
    algorithm character varying(64) NOT NULL,
    algorithm_version character varying(64) NOT NULL,
    model_key character varying(512),
    model_revision character varying(64),
    input_digest character varying(64),
    embedding public.halfvec(768) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: asset_search_projections; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.asset_search_projections (
    asset_id uuid NOT NULL,
    dimensions jsonb DEFAULT '{}'::jsonb NOT NULL,
    description text,
    recovery_outlook character varying(32) DEFAULT 'not_assessed'::character varying NOT NULL,
    recovery_actions jsonb DEFAULT '[]'::jsonb NOT NULL,
    assessment_version character varying(64) DEFAULT 'asset-assessment-v3'::character varying NOT NULL,
    destination_recommendations jsonb DEFAULT '{}'::jsonb NOT NULL,
    content_tags jsonb DEFAULT '[]'::jsonb NOT NULL,
    final_summary text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: assets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.assets (
    id uuid NOT NULL,
    instance_id uuid NOT NULL,
    connection_id uuid NOT NULL,
    immich_asset_id character varying(256) NOT NULL,
    immich_owner_user_id character varying(256) NOT NULL,
    media_type character varying(5) NOT NULL,
    current_revision_id uuid,
    access_state character varying(32) NOT NULL,
    first_seen_at timestamp with time zone NOT NULL,
    last_seen_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    analysis_generation integer,
    current_generation_id uuid,
    exclusion_reason character varying(64),
    source_failure_code character varying(128),
    source_failure_count integer DEFAULT 0 NOT NULL,
    source_failure_revision_id uuid,
    source_failure_at timestamp with time zone,
    excluded_at timestamp with time zone,
    analysis_failure_code character varying(128),
    analysis_failure_count integer DEFAULT 0 NOT NULL,
    analysis_failure_revision_id uuid,
    analysis_failure_at timestamp with time zone
);


--
-- Name: audit_access_tokens; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.audit_access_tokens (
    id uuid NOT NULL,
    owner_id uuid NOT NULL,
    name character varying(128) NOT NULL,
    token_hash character varying(64) NOT NULL,
    scopes jsonb NOT NULL,
    expires_at timestamp with time zone,
    last_used_at timestamp with time zone,
    revoked_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: audit_checkpoints; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.audit_checkpoints (
    id uuid NOT NULL,
    event_id uuid NOT NULL,
    event_hash character varying(64) NOT NULL,
    event_count bigint NOT NULL,
    signature character varying(64) NOT NULL,
    key_id character varying(64) NOT NULL,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: audit_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.audit_events (
    id uuid NOT NULL,
    event_type character varying(128) NOT NULL,
    actor_type character varying(32) NOT NULL,
    actor_id character varying(256) NOT NULL,
    target_ref jsonb NOT NULL,
    before_digest character varying(64),
    after_digest character varying(64),
    details jsonb NOT NULL,
    correlation_id character varying(128) NOT NULL,
    previous_hash character varying(64) NOT NULL,
    event_hash character varying(64) NOT NULL,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: auth_challenges; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.auth_challenges (
    id uuid NOT NULL,
    purpose character varying(32) NOT NULL,
    username character varying(128) NOT NULL,
    challenge bytea NOT NULL,
    user_handle bytea,
    expires_at timestamp with time zone NOT NULL,
    consumed_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: campaign_policy_versions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.campaign_policy_versions (
    id uuid NOT NULL,
    campaign_id uuid NOT NULL,
    version integer NOT NULL,
    policy_digest character varying(64) NOT NULL,
    rules jsonb NOT NULL,
    candidate_breadth character varying(12) NOT NULL,
    raw_policy character varying(18) NOT NULL,
    automatic_tag_policy jsonb NOT NULL,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: cleanup_campaigns; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.cleanup_campaigns (
    id uuid NOT NULL,
    library_program_id uuid,
    origin character varying(32) NOT NULL,
    name character varying(256) NOT NULL,
    objective text NOT NULL,
    state character varying(10) NOT NULL,
    scope jsonb NOT NULL,
    active_policy_version_id uuid,
    created_by uuid NOT NULL,
    started_at timestamp with time zone,
    completed_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: collection_context_snapshots; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.collection_context_snapshots (
    id uuid NOT NULL,
    asset_id uuid NOT NULL,
    asset_revision_id uuid NOT NULL,
    processor_version character varying(128) NOT NULL,
    status character varying(32) DEFAULT 'partial'::character varying NOT NULL,
    source_facts jsonb DEFAULT '{}'::json NOT NULL,
    limitations jsonb DEFAULT '[]'::json NOT NULL,
    semantic_neighbors jsonb DEFAULT '[]'::json NOT NULL,
    evidence_digest character varying(64),
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: collection_group_members; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.collection_group_members (
    id uuid NOT NULL,
    group_id uuid NOT NULL,
    asset_id uuid NOT NULL,
    asset_revision_id uuid NOT NULL,
    sequence_index integer NOT NULL,
    role character varying(64) DEFAULT 'member'::character varying NOT NULL,
    confidence double precision NOT NULL,
    source_facts jsonb DEFAULT '{}'::json NOT NULL,
    membership_confidence double precision DEFAULT '0'::double precision NOT NULL,
    evidence jsonb DEFAULT '{}'::jsonb NOT NULL
);


--
-- Name: collection_groups; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.collection_groups (
    id uuid NOT NULL,
    connection_id uuid NOT NULL,
    group_type character varying(32) NOT NULL,
    group_key character varying(64) NOT NULL,
    detection_version character varying(128) NOT NULL,
    confidence double precision NOT NULL,
    state character varying(32) DEFAULT 'active'::character varying NOT NULL,
    explanation text NOT NULL,
    source_facts jsonb DEFAULT '{}'::json NOT NULL,
    representative_asset_id uuid,
    manual_representative_asset_id uuid,
    captured_start_at timestamp with time zone,
    captured_end_at timestamp with time zone,
    member_count integer DEFAULT 0 NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: deep_review_backfills; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.deep_review_backfills (
    id uuid NOT NULL,
    program_id uuid NOT NULL,
    created_by uuid NOT NULL,
    idempotency_key character varying(128) NOT NULL,
    state character varying(32) NOT NULL,
    coverage character varying(32) NOT NULL,
    policy_revision integer NOT NULL,
    minimum_primary_rating integer NOT NULL,
    exception_policy jsonb NOT NULL,
    prompt_version character varying(128) NOT NULL,
    selection_cutoff timestamp with time zone NOT NULL,
    cursor_completed_at timestamp with time zone,
    cursor_run_id uuid,
    eligible_count bigint DEFAULT '0'::bigint NOT NULL,
    scanned_count bigint DEFAULT '0'::bigint NOT NULL,
    planned_task_count bigint DEFAULT '0'::bigint NOT NULL,
    skipped_count bigint DEFAULT '0'::bigint NOT NULL,
    completed_task_count bigint DEFAULT '0'::bigint NOT NULL,
    failed_task_count bigint DEFAULT '0'::bigint NOT NULL,
    last_error_class character varying(128),
    completed_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: deep_review_prompt_comparisons; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.deep_review_prompt_comparisons (
    id uuid NOT NULL,
    experiment_id uuid NOT NULL,
    asset_id uuid NOT NULL,
    actor_id uuid NOT NULL,
    preferred_prompt_version character varying(128),
    tie boolean DEFAULT false NOT NULL,
    usefulness_notes jsonb NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: deep_review_prompt_experiments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.deep_review_prompt_experiments (
    id uuid NOT NULL,
    created_by uuid NOT NULL,
    model_version_id uuid NOT NULL,
    state character varying(32) NOT NULL,
    prompt_versions jsonb NOT NULL,
    sample_size integer NOT NULL,
    selected_asset_ids jsonb NOT NULL,
    queued_task_count integer DEFAULT 0 NOT NULL,
    completed_task_count integer DEFAULT 0 NOT NULL,
    failed_task_count integer DEFAULT 0 NOT NULL,
    winner_prompt_version character varying(128),
    completed_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    singleton_key character varying(32) DEFAULT 'deep-curation'::character varying NOT NULL
);


--
-- Name: deep_review_prompt_results; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.deep_review_prompt_results (
    id uuid NOT NULL,
    experiment_id uuid NOT NULL,
    asset_id uuid NOT NULL,
    asset_revision_id uuid NOT NULL,
    task_id uuid,
    prompt_version character varying(128) NOT NULL,
    status character varying(32) NOT NULL,
    result jsonb NOT NULL,
    proposed_rating integer,
    confidence double precision,
    duration_ms bigint,
    error_class character varying(128),
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    result_digest character varying(64),
    evidence_provenance jsonb DEFAULT '{}'::jsonb NOT NULL
);


--
-- Name: ensemble_student_training_runs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ensemble_student_training_runs (
    id uuid NOT NULL,
    trigger character varying(32) NOT NULL,
    state character varying(32) NOT NULL,
    requested_by uuid,
    eligible_sample_count integer DEFAULT 0 NOT NULL,
    model_version_id uuid,
    created_model boolean DEFAULT false NOT NULL,
    duration_ms bigint DEFAULT '0'::bigint NOT NULL,
    message text NOT NULL,
    error_class character varying(256),
    started_at timestamp with time zone NOT NULL,
    completed_at timestamp with time zone,
    CONSTRAINT ck_ensemble_student_training_runs_valid_student_run_duration CHECK ((duration_ms >= 0)),
    CONSTRAINT ck_ensemble_student_training_runs_valid_student_run_sam_851d CHECK ((eligible_sample_count >= 0))
);


--
-- Name: ensemble_student_versions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ensemble_student_versions (
    id uuid NOT NULL,
    version integer NOT NULL,
    algorithm character varying(128) NOT NULL,
    feature_schema jsonb NOT NULL,
    target_schema jsonb NOT NULL,
    artifact jsonb NOT NULL,
    artifact_digest character varying(128) NOT NULL,
    training_data_digest character varying(128) NOT NULL,
    sample_count integer NOT NULL,
    metrics jsonb NOT NULL,
    status character varying(32) NOT NULL,
    trained_through timestamp with time zone NOT NULL,
    activated_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    CONSTRAINT ck_ensemble_student_versions_valid_ensemble_student_sam_b82d CHECK ((sample_count >= 0))
);


--
-- Name: highlight_comparisons; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.highlight_comparisons (
    id uuid NOT NULL,
    task_id uuid,
    generation character varying(64) NOT NULL,
    round_kind character varying(32) NOT NULL,
    model_key character varying(256) NOT NULL,
    prompt_version character varying(128) NOT NULL,
    candidate_ids jsonb NOT NULL,
    shuffle_seed character varying(64) NOT NULL,
    state character varying(32) DEFAULT 'queued'::character varying NOT NULL,
    ranking jsonb DEFAULT '[]'::jsonb NOT NULL,
    confidence double precision,
    none_are_highlights boolean,
    explanation text,
    runtime character varying(128),
    model_reported_version character varying(512),
    duration_ms integer,
    completed_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    input_provenance jsonb DEFAULT '[]'::jsonb NOT NULL,
    result_digest character varying(64),
    evidence_provenance jsonb DEFAULT '{}'::jsonb NOT NULL
)
WITH (autovacuum_vacuum_scale_factor='0.05', autovacuum_vacuum_threshold='100', autovacuum_analyze_scale_factor='0.02', autovacuum_analyze_threshold='50');


--
-- Name: highlight_league_entries; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.highlight_league_entries (
    asset_id uuid NOT NULL,
    asset_revision_id uuid NOT NULL,
    campaign_id uuid,
    generation character varying(64) NOT NULL,
    category character varying(32) DEFAULT 'general'::character varying NOT NULL,
    source_rating integer NOT NULL,
    eligibility_reason character varying(128) NOT NULL,
    state character varying(32) DEFAULT 'candidate'::character varying NOT NULL,
    mu double precision DEFAULT 25.0 NOT NULL,
    sigma double precision DEFAULT 8.333 NOT NULL,
    conservative_score double precision DEFAULT 8.334 NOT NULL,
    global_percentile double precision DEFAULT 0.0 NOT NULL,
    match_count integer DEFAULT 0 NOT NULL,
    four_star_match_count integer DEFAULT 0 CONSTRAINT highlight_league_entries_finalist_match_count_not_null NOT NULL,
    qwen_final_queued boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    quality_sum double precision DEFAULT 0.0 NOT NULL,
    quality_count integer DEFAULT 0 NOT NULL,
    in_four_star_pass boolean DEFAULT false CONSTRAINT highlight_league_entries_screening_selected_not_null NOT NULL,
    five_star_batch integer,
    five_star_state character varying(32) DEFAULT 'not_qualified'::character varying CONSTRAINT highlight_league_entries_championship_state_not_null NOT NULL,
    five_star_rank integer,
    relative_rating_proposal_id uuid,
    promoted_at timestamp with time zone,
    analysis_generation_id uuid NOT NULL,
    similar_match_count integer DEFAULT 0 NOT NULL,
    mixed_match_count integer DEFAULT 0 NOT NULL,
    opponent_strength double precision DEFAULT '0'::double precision NOT NULL,
    position_bias double precision DEFAULT '0'::double precision NOT NULL,
    ranking_version character varying(64) DEFAULT 'regularized-plackett-luce-v1'::character varying NOT NULL,
    CONSTRAINT highlight_league_entries_source_rating_check CHECK (((source_rating >= 1) AND (source_rating <= 5)))
);


--
-- Name: human_decisions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.human_decisions (
    id uuid NOT NULL,
    actor_id uuid NOT NULL,
    campaign_id uuid,
    target_type character varying(32) NOT NULL,
    target_id uuid NOT NULL,
    decision_type character varying(17) NOT NULL,
    value jsonb NOT NULL,
    expected_revision integer NOT NULL,
    supersedes_id uuid,
    idempotency_key character varying(128) NOT NULL,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: immich_connections; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.immich_connections (
    id uuid NOT NULL,
    instance_id uuid NOT NULL,
    immich_user_id character varying(256) NOT NULL,
    display_label character varying(256) NOT NULL,
    purpose character varying(6) NOT NULL,
    permission_snapshot jsonb NOT NULL,
    read_policy jsonb NOT NULL,
    write_policy jsonb NOT NULL,
    processing_profile character varying(128) NOT NULL,
    model_profile character varying(128) NOT NULL,
    sync_cursor text,
    health character varying(9) NOT NULL,
    last_sync_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    sync_lease_id uuid,
    sync_lease_expires_at timestamp with time zone
);


--
-- Name: immich_instances; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.immich_instances (
    id uuid NOT NULL,
    stable_instance_id character varying(256) NOT NULL,
    base_url character varying(2048) NOT NULL,
    server_version character varying(64),
    tls_fingerprint character varying(128),
    health character varying(9) NOT NULL,
    last_checked_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: inference_runs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.inference_runs (
    id uuid NOT NULL,
    task_id uuid,
    analysis_run_id uuid,
    analysis_generation_id uuid,
    asset_id uuid,
    model_version_id uuid,
    invocation_index integer NOT NULL,
    phase character varying(64) NOT NULL,
    model_key character varying(512) NOT NULL,
    model_reported_version character varying(512) NOT NULL,
    runtime character varying(128) NOT NULL,
    prompt_version character varying(128) NOT NULL,
    schema_version character varying(128) NOT NULL,
    input_digest character varying(64) NOT NULL,
    output_digest character varying(64) NOT NULL,
    image_count integer NOT NULL,
    request_count integer NOT NULL,
    input_tokens bigint,
    output_tokens bigint,
    duration_ms bigint NOT NULL,
    estimated_cost_microunits bigint,
    cost_basis jsonb NOT NULL,
    state character varying(32) NOT NULL,
    error_class character varying(128),
    completed_at timestamp with time zone NOT NULL
)
WITH (autovacuum_vacuum_scale_factor='0.05', autovacuum_vacuum_threshold='100', autovacuum_analyze_scale_factor='0.02', autovacuum_analyze_threshold='50');


--
-- Name: library_policy_versions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.library_policy_versions (
    id uuid NOT NULL,
    program_id uuid NOT NULL,
    version integer NOT NULL,
    policy_digest character varying(64) NOT NULL,
    policy jsonb NOT NULL,
    created_by uuid,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: library_programs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.library_programs (
    id uuid NOT NULL,
    name character varying(256) NOT NULL,
    state character varying(8) NOT NULL,
    enabled boolean NOT NULL,
    revision integer NOT NULL,
    active_policy_version_id uuid,
    pipeline_version character varying(128) NOT NULL,
    sync_interval_seconds integer NOT NULL,
    queue_low_watermark integer NOT NULL,
    queue_high_watermark integer NOT NULL,
    max_batch_assets integer NOT NULL,
    last_planned_at timestamp with time zone,
    last_error_class character varying(128),
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    selection_order character varying(32) DEFAULT 'newest'::character varying NOT NULL,
    ingest_new_assets boolean DEFAULT false NOT NULL,
    completion_batch_size integer DEFAULT 10 NOT NULL,
    cohort_mode character varying(16) DEFAULT 'automatic'::character varying NOT NULL,
    requested_asset_count integer,
    admitted_asset_count integer DEFAULT 0 NOT NULL,
    finalized_asset_count integer DEFAULT 0 NOT NULL,
    random_seed character varying(64),
    source_cursor integer DEFAULT 0 NOT NULL,
    library_generation integer DEFAULT 1 NOT NULL,
    run_started_at timestamp with time zone,
    deep_review_min_rating integer DEFAULT 4 NOT NULL,
    deep_review_exceptions jsonb DEFAULT jsonb_build_object('low_confidence', true, 'technical_disagreement', true, 'privacy_ambiguity', true, 'recoverable_two_star', true, 'ordinary_three_star', true, 'protected_one_star', true) NOT NULL,
    active_deep_review_prompt character varying(128) DEFAULT 'deep-review-v2'::character varying NOT NULL,
    work_scope character varying(16) DEFAULT 'immich'::character varying NOT NULL,
    model_pipeline_policy jsonb DEFAULT '{"aesthetic_siglip": true, "fgaesq": true, "q_realign_mini": false, "ernie_image_aes": false, "unipercept": true, "gemma_verifier": true, "gemma_gate": "passed"}'::json NOT NULL,
    review_generation integer NOT NULL,
    auto_discover_new_assets boolean DEFAULT false NOT NULL,
    source_exhausted boolean DEFAULT false NOT NULL,
    model_block_target_minutes integer NOT NULL,
    source_total_count integer DEFAULT 0 NOT NULL,
    ranking_dirty_at timestamp with time zone,
    ranking_last_published_at timestamp with time zone,
    ranking_dirty_count integer NOT NULL,
    ranking_force_refresh boolean NOT NULL,
    original_requested_asset_count integer,
    excluded_asset_count integer DEFAULT 0 NOT NULL,
    CONSTRAINT ck_library_programs_ck_library_programs_model_block_minutes CHECK (((model_block_target_minutes >= 15) AND (model_block_target_minutes <= 30))),
    CONSTRAINT ck_library_programs_ck_library_programs_original_reques_f677 CHECK (((original_requested_asset_count IS NULL) OR (original_requested_asset_count >= 1))),
    CONSTRAINT ck_library_programs_ck_library_programs_ranking_dirty_count CHECK ((ranking_dirty_count >= 0)),
    CONSTRAINT ck_library_programs_ck_library_programs_source_total_count CHECK ((source_total_count >= 0)),
    CONSTRAINT ck_library_programs_valid_library_admitted_asset_count CHECK ((admitted_asset_count >= 0)),
    CONSTRAINT ck_library_programs_valid_library_completion_batch_size CHECK (((completion_batch_size >= 1) AND (completion_batch_size <= 100))),
    CONSTRAINT ck_library_programs_valid_library_finalized_asset_count CHECK ((finalized_asset_count >= 0)),
    CONSTRAINT ck_library_programs_valid_library_requested_asset_count CHECK (((requested_asset_count IS NULL) OR (requested_asset_count >= 1)))
);


--
-- Name: library_ranking_runs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.library_ranking_runs (
    id uuid NOT NULL,
    version integer NOT NULL,
    algorithm_version character varying(128) NOT NULL,
    reason character varying(64) NOT NULL,
    ranked_asset_count integer NOT NULL,
    comparison_count integer NOT NULL,
    evidence_digest character varying(64) NOT NULL,
    details jsonb NOT NULL,
    notes text,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: library_run_schedules; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.library_run_schedules (
    id uuid NOT NULL,
    program_id uuid NOT NULL,
    enabled boolean NOT NULL,
    revision integer NOT NULL,
    timezone character varying(64) NOT NULL,
    weekday integer NOT NULL,
    local_time character varying(5) NOT NULL,
    work_scope character varying(16) NOT NULL,
    selection_order character varying(32) NOT NULL,
    requested_asset_count integer,
    next_due_at timestamp with time zone,
    pending_due_at timestamp with time zone,
    last_due_at timestamp with time zone,
    last_started_at timestamp with time zone,
    last_completed_at timestamp with time zone,
    last_result character varying(32),
    active_library_generation integer,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    CONSTRAINT ck_library_run_schedules_valid_library_schedule_requested_count CHECK (((requested_asset_count IS NULL) OR (requested_asset_count >= 1))),
    CONSTRAINT ck_library_run_schedules_valid_library_schedule_weekday CHECK (((weekday >= 0) AND (weekday <= 6)))
);


--
-- Name: model_deployments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.model_deployments (
    id uuid NOT NULL,
    model_package_id uuid NOT NULL,
    worker_id uuid NOT NULL,
    runtime character varying(64) NOT NULL,
    state character varying(32) NOT NULL,
    health character varying(32) NOT NULL,
    loaded boolean NOT NULL,
    benchmark_eligible boolean NOT NULL,
    last_checked_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: model_packages; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.model_packages (
    id uuid NOT NULL,
    model_version_id uuid NOT NULL,
    source_url character varying(2048) NOT NULL,
    source_revision character varying(256) NOT NULL,
    artifact_checksum character varying(128) NOT NULL,
    license_id character varying(128) NOT NULL,
    runtime_format character varying(64) NOT NULL,
    size_bytes bigint NOT NULL,
    relative_path character varying(2048),
    state character varying(32) NOT NULL,
    approved_by uuid NOT NULL,
    verified_at timestamp with time zone,
    last_accessed_at timestamp with time zone,
    is_pinned boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: model_versions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.model_versions (
    id uuid NOT NULL,
    model_id character varying(512) NOT NULL,
    version character varying(256) NOT NULL,
    runtime character varying(128) NOT NULL,
    quantization character varying(64) NOT NULL,
    license_id character varying(128),
    artifact_checksum character varying(128),
    capabilities jsonb NOT NULL,
    resource_profile jsonb NOT NULL,
    benchmark jsonb NOT NULL,
    enabled boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: normalized_derivatives; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.normalized_derivatives (
    id uuid NOT NULL,
    asset_id uuid NOT NULL,
    asset_revision_id uuid NOT NULL,
    variant character varying(32) NOT NULL,
    transform_version character varying(64) NOT NULL,
    content_digest character varying(64) NOT NULL,
    relative_path character varying(256) NOT NULL,
    media_type character varying(32) NOT NULL,
    size_bytes bigint NOT NULL,
    width integer NOT NULL,
    height integer NOT NULL,
    encoding_quality integer NOT NULL,
    last_accessed_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: operational_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.operational_events (
    id bigint NOT NULL,
    event_id uuid NOT NULL,
    severity character varying(16) NOT NULL,
    source character varying(32) NOT NULL,
    event_type character varying(128) NOT NULL,
    message character varying(512) NOT NULL,
    correlation_id character varying(128),
    task_id uuid,
    worker_id uuid,
    model_id uuid,
    connection_id uuid,
    details jsonb NOT NULL,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: operational_events_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.operational_events_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: operational_events_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.operational_events_id_seq OWNED BY public.operational_events.id;


--
-- Name: owner_rating_session_items; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.owner_rating_session_items (
    id uuid NOT NULL,
    session_id uuid NOT NULL,
    "position" integer NOT NULL,
    asset_id uuid NOT NULL,
    asset_revision_id uuid NOT NULL,
    hidden_model_rating integer NOT NULL,
    hidden_model_confidence double precision NOT NULL,
    selection_facts jsonb NOT NULL,
    expected_rating_decision_id uuid,
    expected_sensitive_decision_id uuid,
    state character varying(32) NOT NULL,
    submitted_rating integer,
    rating_decision_id uuid,
    sensitive_decision_id uuid,
    advance_idempotency_key character varying(128),
    rated_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL,
    CONSTRAINT ck_owner_rating_session_items_ck_owner_rating_item_confidence CHECK (((hidden_model_confidence >= (0)::double precision) AND (hidden_model_confidence <= (1)::double precision))),
    CONSTRAINT ck_owner_rating_session_items_ck_owner_rating_item_model_rating CHECK (((hidden_model_rating >= 1) AND (hidden_model_rating <= 5))),
    CONSTRAINT ck_owner_rating_session_items_ck_owner_rating_item_position CHECK ((("position" >= 0) AND ("position" < 100))),
    CONSTRAINT ck_owner_rating_session_items_ck_owner_rating_item_subm_18ff CHECK (((submitted_rating IS NULL) OR ((submitted_rating >= 1) AND (submitted_rating <= 5))))
);


--
-- Name: owner_rating_sessions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.owner_rating_sessions (
    id uuid NOT NULL,
    owner_id uuid NOT NULL,
    state character varying(32) NOT NULL,
    requested_count integer NOT NULL,
    selected_count integer NOT NULL,
    current_position integer NOT NULL,
    reveal_after_save boolean NOT NULL,
    selection_policy jsonb NOT NULL,
    selection_digest character varying(64) NOT NULL,
    idempotency_key character varying(128) NOT NULL,
    completed_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    CONSTRAINT ck_owner_rating_sessions_ck_owner_rating_session_curren_4fd4 CHECK (((current_position >= 0) AND (current_position <= selected_count))),
    CONSTRAINT ck_owner_rating_sessions_ck_owner_rating_session_reques_f9f2 CHECK (((requested_count >= 1) AND (requested_count <= 100))),
    CONSTRAINT ck_owner_rating_sessions_ck_owner_rating_session_selected_count CHECK (((selected_count >= 0) AND (selected_count <= requested_count)))
);


--
-- Name: passkey_credentials; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.passkey_credentials (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    credential_id character varying(1024) NOT NULL,
    public_key bytea NOT NULL,
    sign_count integer NOT NULL,
    transports jsonb NOT NULL,
    name character varying(128) NOT NULL,
    last_used_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: person_references; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.person_references (
    id uuid NOT NULL,
    connection_id uuid NOT NULL,
    immich_person_id character varying(256) NOT NULL,
    immich_owner_user_id character varying(256) NOT NULL,
    encrypted_display_name bytea NOT NULL,
    is_favorite boolean NOT NULL,
    is_hidden boolean NOT NULL,
    access_state character varying(32) NOT NULL,
    source_data jsonb NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: personal_ranker_versions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.personal_ranker_versions (
    id uuid NOT NULL,
    version integer NOT NULL,
    algorithm character varying(128) NOT NULL,
    feature_schema jsonb NOT NULL,
    artifact jsonb NOT NULL,
    artifact_digest character varying(128) NOT NULL,
    training_data_digest character varying(128) NOT NULL,
    l2_penalty double precision NOT NULL,
    sample_count integer NOT NULL,
    label_distribution jsonb NOT NULL,
    metrics jsonb NOT NULL,
    status character varying(32) NOT NULL,
    created_by uuid NOT NULL,
    source_decision_max_created_at timestamp with time zone CONSTRAINT personal_ranker_versions_source_decision_max_created_a_not_null NOT NULL,
    activated_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    evidence_provenance jsonb DEFAULT '{}'::jsonb NOT NULL,
    CONSTRAINT ck_personal_ranker_versions_valid_sample_count CHECK ((sample_count >= 0))
);


--
-- Name: preference_lora_adapters; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.preference_lora_adapters (
    id uuid NOT NULL,
    version integer NOT NULL,
    run_id uuid NOT NULL,
    status character varying(32) NOT NULL,
    base_model_key character varying(256) NOT NULL,
    dataset_digest character varying(71) NOT NULL,
    artifact_digest character varying(71) NOT NULL,
    artifact_relative_path character varying(512) NOT NULL,
    artifact_bytes bigint NOT NULL,
    metrics jsonb NOT NULL,
    approved_by uuid,
    approved_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: preference_lora_runs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.preference_lora_runs (
    id uuid NOT NULL,
    idempotency_key character varying(128) NOT NULL,
    created_by uuid NOT NULL,
    state character varying(32) NOT NULL,
    base_model_key character varying(256) NOT NULL,
    label_count integer NOT NULL,
    label_distribution jsonb NOT NULL,
    train_count integer NOT NULL,
    validation_count integer NOT NULL,
    test_count integer NOT NULL,
    dataset_digest character varying(71) NOT NULL,
    dataset_relative_path character varying(512) NOT NULL,
    dataset_bytes bigint NOT NULL,
    task_id uuid,
    metrics jsonb NOT NULL,
    error_class character varying(128),
    started_at timestamp with time zone,
    completed_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    result_digest character varying(64),
    evidence_provenance jsonb DEFAULT '{}'::jsonb NOT NULL
);


--
-- Name: preference_photo_insights; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.preference_photo_insights (
    id uuid NOT NULL,
    run_id uuid NOT NULL,
    asset_id uuid NOT NULL,
    asset_revision_id uuid NOT NULL,
    owner_decision_id uuid NOT NULL,
    analysis_run_id uuid,
    automatic_review_digest character varying(64) NOT NULL,
    task_id uuid,
    state character varying(32) NOT NULL,
    owner_rating integer NOT NULL,
    automatic_rating integer,
    insight jsonb NOT NULL,
    error_class character varying(128),
    completed_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    result_digest character varying(64),
    evidence_provenance jsonb DEFAULT '{}'::jsonb NOT NULL
);


--
-- Name: preference_prompt_runs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.preference_prompt_runs (
    id uuid NOT NULL,
    created_by uuid NOT NULL,
    idempotency_key character varying(128) NOT NULL,
    personal_ranker_version_id uuid,
    state character varying(32) NOT NULL,
    source_prompt_digest character varying(64) NOT NULL,
    source_prompt_version integer NOT NULL,
    photo_count integer NOT NULL,
    completed_photo_count integer NOT NULL,
    failed_photo_count integer NOT NULL,
    synthesis_task_id uuid,
    proposed_revision_id uuid,
    completed_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    result_digest character varying(64),
    evidence_provenance jsonb DEFAULT '{}'::jsonb NOT NULL
);


--
-- Name: processing_batches; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.processing_batches (
    id uuid NOT NULL,
    program_id uuid NOT NULL,
    campaign_id uuid,
    state character varying(9) NOT NULL,
    cursor_start text,
    cursor_end text,
    asset_count integer NOT NULL,
    planned_task_count integer NOT NULL,
    terminal_task_count integer NOT NULL,
    checkpoint jsonb NOT NULL,
    pipeline_version character varying(128) NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: proposals; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.proposals (
    id uuid NOT NULL,
    campaign_id uuid NOT NULL,
    proposal_type character varying(17) NOT NULL,
    target_type character varying(32) NOT NULL,
    target_id uuid NOT NULL,
    value jsonb NOT NULL,
    confidence double precision NOT NULL,
    reason_codes jsonb NOT NULL,
    explanation text NOT NULL,
    state character varying(10) NOT NULL,
    analysis_version character varying(128) NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    analysis_generation_id uuid,
    authority_stage character varying(32)
)
WITH (autovacuum_vacuum_scale_factor='0.05', autovacuum_vacuum_threshold='100', autovacuum_analyze_scale_factor='0.02', autovacuum_analyze_threshold='50');


--
-- Name: repeatability_benchmarks; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.repeatability_benchmarks (
    id uuid NOT NULL,
    model_key character varying(512) NOT NULL,
    task character varying(128) NOT NULL,
    inference_profile character varying(128) NOT NULL,
    prompt_version character varying(128) NOT NULL,
    fixed_seed_samples integer NOT NULL,
    varied_seed_samples integer NOT NULL,
    schema_success_rate double precision NOT NULL,
    exact_rating_rate double precision NOT NULL,
    mean_absolute_deviation double precision NOT NULL,
    contradiction_rate double precision NOT NULL,
    stable boolean NOT NULL,
    details jsonb NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: resource_pressure_samples; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.resource_pressure_samples (
    id bigint NOT NULL,
    source character varying(32) NOT NULL,
    state character varying(32) NOT NULL,
    payload jsonb NOT NULL,
    sampled_at timestamp with time zone NOT NULL
);


--
-- Name: resource_pressure_samples_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.resource_pressure_samples_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: resource_pressure_samples_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.resource_pressure_samples_id_seq OWNED BY public.resource_pressure_samples.id;


--
-- Name: review_prompt_overrides; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.review_prompt_overrides (
    id uuid NOT NULL,
    prompt_key character varying(128) NOT NULL,
    instruction text NOT NULL,
    revision integer NOT NULL,
    updated_by uuid,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: review_prompt_revisions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.review_prompt_revisions (
    id uuid NOT NULL,
    prompt_key character varying(128) NOT NULL,
    version integer NOT NULL,
    instruction text NOT NULL,
    instruction_digest character varying(64) NOT NULL,
    source character varying(32) NOT NULL,
    status character varying(32) NOT NULL,
    change_summary character varying(512),
    evidence jsonb NOT NULL,
    personal_ranker_version_id uuid,
    created_by uuid,
    activated_by uuid,
    activated_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: review_reanalysis_runs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.review_reanalysis_runs (
    id uuid NOT NULL,
    program_id uuid NOT NULL,
    requested_by uuid,
    review_generation integer NOT NULL,
    scope character varying(32) NOT NULL,
    reason character varying(128) NOT NULL,
    state character varying(32) NOT NULL,
    selection_cutoff timestamp with time zone NOT NULL,
    cursor_created_at timestamp with time zone,
    cursor_asset_id uuid,
    scanned_count bigint NOT NULL,
    planned_task_count bigint NOT NULL,
    skipped_count bigint NOT NULL,
    completed_task_count bigint NOT NULL,
    failed_task_count bigint NOT NULL,
    completed_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    mode character varying(32) DEFAULT 'complete_review'::character varying NOT NULL,
    start_stage character varying(64) DEFAULT 'source'::character varying NOT NULL,
    reuse_policy character varying(32) DEFAULT 'compatible'::character varying NOT NULL,
    publication_policy character varying(32) DEFAULT 'atomic'::character varying NOT NULL,
    selection jsonb DEFAULT '{}'::jsonb NOT NULL,
    request_digest character varying(64) NOT NULL,
    expanded_stages jsonb DEFAULT '[]'::jsonb NOT NULL,
    excluded_task_count bigint DEFAULT '0'::bigint NOT NULL
);


--
-- Name: search_backfill_runs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.search_backfill_runs (
    id uuid NOT NULL,
    created_by uuid NOT NULL,
    idempotency_key character varying(64) NOT NULL,
    request_digest character varying(64) NOT NULL,
    singleton_key character varying(16) NOT NULL,
    state character varying(32) NOT NULL,
    requested_count integer NOT NULL,
    "order" character varying(32) NOT NULL,
    queued_count bigint NOT NULL,
    cancelled_count bigint NOT NULL,
    completed_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: service_heartbeats; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.service_heartbeats (
    id uuid NOT NULL,
    service_key character varying(64) NOT NULL,
    hostname_label character varying(256) NOT NULL,
    role character varying(64) NOT NULL,
    status character varying(32) NOT NULL,
    software_version character varying(64) NOT NULL,
    details jsonb NOT NULL,
    started_at timestamp with time zone NOT NULL,
    last_heartbeat timestamp with time zone NOT NULL
);


--
-- Name: sessions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.sessions (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    token_hash character varying(64) NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    reauthenticated_at timestamp with time zone,
    revoked_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: similarity_group_members; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.similarity_group_members (
    id uuid NOT NULL,
    group_id uuid NOT NULL,
    asset_id uuid NOT NULL,
    role character varying(64) NOT NULL,
    rank integer,
    confidence double precision NOT NULL,
    is_protected boolean NOT NULL,
    protection_reasons jsonb NOT NULL
);


--
-- Name: similarity_groups; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.similarity_groups (
    id uuid NOT NULL,
    library_program_id uuid,
    campaign_id uuid NOT NULL,
    group_key character varying(64) NOT NULL,
    group_type character varying(18) NOT NULL,
    group_version integer NOT NULL,
    confidence double precision NOT NULL,
    estimated_recoverable_bytes bigint NOT NULL,
    explanation text NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: storage_inventories; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.storage_inventories (
    scope character varying(64) NOT NULL,
    scan_started_at timestamp with time zone NOT NULL,
    scanned_at timestamp with time zone NOT NULL,
    scan_duration_ms bigint NOT NULL,
    is_complete boolean NOT NULL,
    artifact_logical_bytes bigint NOT NULL,
    artifact_allocated_bytes bigint NOT NULL,
    artifact_file_count bigint NOT NULL,
    mapped_logical_bytes bigint NOT NULL,
    mapped_allocated_bytes bigint NOT NULL,
    mapped_file_count bigint NOT NULL,
    orphan_logical_bytes bigint NOT NULL,
    orphan_allocated_bytes bigint NOT NULL,
    orphan_file_count bigint NOT NULL,
    partial_logical_bytes bigint NOT NULL,
    partial_allocated_bytes bigint NOT NULL,
    partial_file_count bigint NOT NULL,
    unknown_logical_bytes bigint NOT NULL,
    unknown_allocated_bytes bigint NOT NULL,
    unknown_file_count bigint NOT NULL,
    missing_mapping_bytes bigint NOT NULL,
    missing_mapping_count bigint NOT NULL,
    mismatched_file_count bigint NOT NULL,
    symlink_count bigint NOT NULL,
    scan_error_count bigint NOT NULL,
    cleanup_deleted_bytes bigint NOT NULL,
    cleanup_deleted_files bigint NOT NULL,
    registered_model_package_bytes bigint NOT NULL,
    coordinator_model_logical_bytes bigint NOT NULL,
    coordinator_model_allocated_bytes bigint NOT NULL,
    coordinator_model_file_count bigint NOT NULL,
    coordinator_model_missing_count bigint NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: tag_references; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tag_references (
    id uuid NOT NULL,
    connection_id uuid NOT NULL,
    immich_tag_id character varying(256) NOT NULL,
    name character varying(512) NOT NULL,
    value character varying(2048) NOT NULL,
    immich_parent_id character varying(256),
    access_state character varying(32) NOT NULL,
    source_data jsonb NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: task_failure_evidence; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.task_failure_evidence (
    id uuid NOT NULL,
    task_id uuid,
    analysis_generation_id uuid,
    asset_id uuid,
    asset_revision_id uuid,
    worker_id uuid,
    model_version_id uuid,
    task_type character varying(128) NOT NULL,
    attempt_number integer NOT NULL,
    error_class character varying(128) NOT NULL,
    retryable boolean NOT NULL,
    reason_digest character varying(64),
    input_digest character varying(64),
    output_digest character varying(64),
    duration_ms bigint,
    model_key character varying(512),
    prompt_version character varying(128),
    schema_version character varying(128),
    inference_profile jsonb DEFAULT '{}'::jsonb NOT NULL,
    application_release character varying(32) DEFAULT 'legacy-unrecorded'::character varying NOT NULL,
    release_provenance jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: tasks; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tasks (
    id uuid NOT NULL,
    temporal_workflow_id character varying(256),
    campaign_id uuid,
    parent_task_id uuid,
    processing_batch_id uuid,
    connection_id uuid,
    asset_id uuid,
    asset_revision_id uuid,
    task_type character varying(128) NOT NULL,
    routing_key character varying(64) NOT NULL,
    data_class character varying(32) NOT NULL,
    estimated_work_class character varying(32) NOT NULL,
    target_ref jsonb NOT NULL,
    requirements jsonb NOT NULL,
    idempotency_key character varying(128) NOT NULL,
    state character varying(16) NOT NULL,
    progress jsonb NOT NULL,
    checkpoint jsonb NOT NULL,
    priority integer NOT NULL,
    attempt_count integer NOT NULL,
    max_attempts integer NOT NULL,
    leased_to_worker_id uuid,
    lease_id uuid,
    lease_expires_at timestamp with time zone,
    available_at timestamp with time zone NOT NULL,
    cancel_requested_at timestamp with time zone,
    last_error_class character varying(128),
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    completion_role character varying(32) DEFAULT 'core'::character varying NOT NULL,
    analysis_generation_id uuid
)
WITH (autovacuum_vacuum_scale_factor='0.02', autovacuum_vacuum_threshold='50', autovacuum_analyze_scale_factor='0.01', autovacuum_analyze_threshold='50');


--
-- Name: users; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.users (
    id uuid NOT NULL,
    username character varying(128) NOT NULL,
    display_name character varying(256) NOT NULL,
    role character varying(13) NOT NULL,
    is_active boolean NOT NULL,
    password_hash text,
    failed_password_attempts integer NOT NULL,
    password_locked_until timestamp with time zone,
    last_password_login_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: visual_embeddings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.visual_embeddings (
    id uuid NOT NULL,
    asset_id uuid NOT NULL,
    asset_revision_id uuid NOT NULL,
    analysis_run_id uuid NOT NULL,
    algorithm character varying(64) NOT NULL,
    algorithm_version character varying(64) NOT NULL,
    embedding public.halfvec(384) NOT NULL,
    created_at timestamp with time zone NOT NULL,
    analysis_generation_id uuid,
    purpose character varying(64) NOT NULL,
    model_key character varying(512),
    model_revision character varying(64),
    input_digest character varying(64),
    CONSTRAINT ck_visual_embeddings_ck_visual_embedding_not_grid CHECK ((((algorithm)::text <> 'local-grid'::text) AND ((algorithm_version)::text <> 'rgb-grid-384-v1'::text)))
)
WITH (autovacuum_vacuum_scale_factor='0.05', autovacuum_vacuum_threshold='100', autovacuum_analyze_scale_factor='0.02', autovacuum_analyze_threshold='50');


--
-- Name: visual_fingerprint_bands; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.visual_fingerprint_bands (
    id uuid NOT NULL,
    fingerprint_id uuid NOT NULL,
    asset_id uuid NOT NULL,
    algorithm_version character varying(64) NOT NULL,
    band_index integer NOT NULL,
    band_value character varying(8) NOT NULL
);


--
-- Name: visual_fingerprints; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.visual_fingerprints (
    id uuid NOT NULL,
    asset_id uuid NOT NULL,
    asset_revision_id uuid NOT NULL,
    analysis_run_id uuid NOT NULL,
    algorithm character varying(64) NOT NULL,
    algorithm_version character varying(64) NOT NULL,
    value text NOT NULL,
    bit_length integer NOT NULL,
    created_at timestamp with time zone NOT NULL,
    similarity_checked_at timestamp with time zone,
    similarity_neighbor_count integer DEFAULT 0 NOT NULL
);


--
-- Name: worker_credentials; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.worker_credentials (
    id uuid NOT NULL,
    worker_id uuid NOT NULL,
    token_hash character varying(64) NOT NULL,
    allowed_routing_keys jsonb NOT NULL,
    allowed_data_classes jsonb NOT NULL,
    allowed_connection_ids jsonb NOT NULL,
    issued_at timestamp with time zone NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    grace_expires_at timestamp with time zone,
    replaced_by_id uuid,
    revoked_at timestamp with time zone
);


--
-- Name: worker_enrollment_tokens; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.worker_enrollment_tokens (
    id uuid NOT NULL,
    token_hash character varying(64) NOT NULL,
    expected_hostname character varying(256) NOT NULL,
    allowed_capabilities jsonb NOT NULL,
    allowed_routing_keys jsonb NOT NULL,
    allowed_data_classes jsonb NOT NULL,
    allowed_connection_ids jsonb NOT NULL,
    created_by uuid NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    consumed_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: worker_model_availability; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.worker_model_availability (
    id uuid NOT NULL,
    worker_id uuid NOT NULL,
    model_version_id uuid NOT NULL,
    available boolean NOT NULL,
    loaded boolean NOT NULL,
    artifact_checksum character varying(128),
    runtime_fingerprint character varying(128) NOT NULL,
    capability character varying(64) NOT NULL,
    runtime_evidence jsonb NOT NULL,
    last_seen_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: workers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.workers (
    id uuid NOT NULL,
    token_hash character varying(64) NOT NULL,
    certificate_subject character varying(512),
    hostname_label character varying(256) NOT NULL,
    architecture character varying(64) NOT NULL,
    operating_system character varying(128) NOT NULL,
    software_version character varying(64) NOT NULL,
    capabilities jsonb NOT NULL,
    runtime_status jsonb NOT NULL,
    allowed_capabilities jsonb NOT NULL,
    state character varying(32) NOT NULL,
    last_heartbeat timestamp with time zone NOT NULL,
    credential_rotated_at timestamp with time zone,
    revoked_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    desired_state character varying(32) DEFAULT 'active'::character varying NOT NULL,
    schedule_policy jsonb DEFAULT '{"enabled": false, "timezone": "Europe/Stockholm", "weekdays": [0, 1, 2, 3, 4, 5, 6], "end_local": "00:00", "start_local": "00:00"}'::jsonb NOT NULL,
    control_revision integer DEFAULT 0 NOT NULL,
    affinity_routing_key character varying(64),
    affinity_model_key character varying(512),
    affinity_processing_batch_id uuid,
    affinity_remaining integer DEFAULT 0 NOT NULL,
    affinity_task_count bigint DEFAULT '0'::bigint NOT NULL,
    affinity_switch_count bigint DEFAULT '0'::bigint NOT NULL,
    affinity_updated_at timestamp with time zone,
    helper_mode_enabled boolean DEFAULT false NOT NULL,
    apple_review_filters jsonb DEFAULT '["automatic"]'::json NOT NULL,
    helper_schedule_policy json,
    last_enrollment_nonce_hash character varying(64),
    model_concurrency_limit integer DEFAULT 4 NOT NULL,
    CONSTRAINT ck_workers_ck_workers_model_concurrency_limit CHECK (((model_concurrency_limit >= 1) AND (model_concurrency_limit <= 4))),
    CONSTRAINT ck_workers_valid_worker_affinity_remaining CHECK ((affinity_remaining >= 0))
);


--
-- Name: workspace_presets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.workspace_presets (
    id uuid NOT NULL,
    owner_id uuid NOT NULL,
    kind character varying(32) NOT NULL,
    name character varying(128) NOT NULL,
    payload jsonb NOT NULL,
    revision integer DEFAULT 1 NOT NULL,
    is_default boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: writeback_batches; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.writeback_batches (
    id uuid NOT NULL,
    created_by uuid NOT NULL,
    operation_class character varying(32) NOT NULL,
    cohort_digest character varying(64),
    policy_version character varying(128),
    required_inspection_count integer NOT NULL,
    completed_inspection_count integer NOT NULL,
    state character varying(9) NOT NULL,
    preview_digest character varying(64),
    approved_at timestamp with time zone,
    approval_session_id uuid,
    expires_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    selection jsonb DEFAULT '{}'::jsonb NOT NULL,
    materialization_state character varying(32) DEFAULT 'pending'::character varying NOT NULL,
    materialization_cursor uuid,
    materialization_digest_state character varying(64),
    materialized_count integer DEFAULT 0 NOT NULL,
    total_candidates integer DEFAULT 0 NOT NULL,
    eligible_count integer DEFAULT 0 NOT NULL,
    protected_count integer DEFAULT 0 NOT NULL,
    already_current_count integer DEFAULT 0 NOT NULL,
    stale_count integer DEFAULT 0 NOT NULL,
    incomplete_count integer DEFAULT 0 NOT NULL,
    breakdown jsonb DEFAULT '{}'::jsonb NOT NULL,
    operation_counts jsonb DEFAULT '{}'::jsonb NOT NULL,
    error_message text,
    cancel_requested_at timestamp with time zone,
    execution_lease_id uuid,
    execution_lease_expires_at timestamp with time zone,
    blocked_reasons jsonb DEFAULT '{}'::jsonb NOT NULL
);


--
-- Name: writeback_operations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.writeback_operations (
    id uuid NOT NULL,
    batch_id uuid NOT NULL,
    connection_id uuid NOT NULL,
    asset_id uuid NOT NULL,
    operation_type character varying(64) NOT NULL,
    before_state jsonb NOT NULL,
    requested_state jsonb NOT NULL,
    after_state jsonb,
    inspection_required boolean NOT NULL,
    risk_reasons jsonb NOT NULL,
    inspected_by uuid,
    inspected_at timestamp with time zone,
    idempotency_key character varying(128) NOT NULL,
    state character varying(9) NOT NULL,
    attempt_count integer NOT NULL,
    asset_revision_id uuid,
    snapshot_updated_at timestamp with time zone,
    evidence_digest character varying(64)
);


--
-- Name: operational_events id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.operational_events ALTER COLUMN id SET DEFAULT nextval('public.operational_events_id_seq'::regclass);


--
-- Name: resource_pressure_samples id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_pressure_samples ALTER COLUMN id SET DEFAULT nextval('public.resource_pressure_samples_id_seq'::regclass);




--
-- Name: asset_ml_scores asset_ml_scores_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.asset_ml_scores
    ADD CONSTRAINT asset_ml_scores_pkey PRIMARY KEY (asset_id);


--
-- Name: asset_pipeline_state asset_pipeline_state_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.asset_pipeline_state
    ADD CONSTRAINT asset_pipeline_state_pkey PRIMARY KEY (asset_id);


--
-- Name: asset_ratings asset_ratings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.asset_ratings
    ADD CONSTRAINT asset_ratings_pkey PRIMARY KEY (asset_id);


--
-- Name: asset_search_projections asset_search_projections_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.asset_search_projections
    ADD CONSTRAINT asset_search_projections_pkey PRIMARY KEY (asset_id);


--
-- Name: highlight_comparisons highlight_comparisons_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.highlight_comparisons
    ADD CONSTRAINT highlight_comparisons_pkey PRIMARY KEY (id);


--
-- Name: highlight_comparisons highlight_comparisons_task_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.highlight_comparisons
    ADD CONSTRAINT highlight_comparisons_task_id_key UNIQUE (task_id);


--
-- Name: highlight_league_entries highlight_league_entries_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.highlight_league_entries
    ADD CONSTRAINT highlight_league_entries_pkey PRIMARY KEY (asset_id);


--
-- Name: album_asset_references pk_album_asset_references; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.album_asset_references
    ADD CONSTRAINT pk_album_asset_references PRIMARY KEY (id);


--
-- Name: album_references pk_album_references; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.album_references
    ADD CONSTRAINT pk_album_references PRIMARY KEY (id);


--
-- Name: analysis_generations pk_analysis_generations; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.analysis_generations
    ADD CONSTRAINT pk_analysis_generations PRIMARY KEY (id);


--
-- Name: analysis_nodes pk_analysis_nodes; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.analysis_nodes
    ADD CONSTRAINT pk_analysis_nodes PRIMARY KEY (id);


--
-- Name: analysis_obligations pk_analysis_obligations; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.analysis_obligations
    ADD CONSTRAINT pk_analysis_obligations PRIMARY KEY (id);


--
-- Name: analysis_publication_snapshots pk_analysis_publication_snapshots; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.analysis_publication_snapshots
    ADD CONSTRAINT pk_analysis_publication_snapshots PRIMARY KEY (id);


--
-- Name: analysis_runs pk_analysis_runs; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.analysis_runs
    ADD CONSTRAINT pk_analysis_runs PRIMARY KEY (id);


--
-- Name: analysis_saved_filters pk_analysis_saved_filters; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.analysis_saved_filters
    ADD CONSTRAINT pk_analysis_saved_filters PRIMARY KEY (id);


--
-- Name: api_credentials pk_api_credentials; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.api_credentials
    ADD CONSTRAINT pk_api_credentials PRIMARY KEY (id);


--
-- Name: apple_vision_evidence pk_apple_vision_evidence; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.apple_vision_evidence
    ADD CONSTRAINT pk_apple_vision_evidence PRIMARY KEY (id);


--
-- Name: asset_curation_snapshots pk_asset_curation_snapshots; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.asset_curation_snapshots
    ADD CONSTRAINT pk_asset_curation_snapshots PRIMARY KEY (asset_id);


--
-- Name: asset_face_references pk_asset_face_references; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.asset_face_references
    ADD CONSTRAINT pk_asset_face_references PRIMARY KEY (id);


--
-- Name: asset_immich_contexts pk_asset_immich_contexts; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.asset_immich_contexts
    ADD CONSTRAINT pk_asset_immich_contexts PRIMARY KEY (asset_id);


--
-- Name: asset_metrics pk_asset_metrics; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.asset_metrics
    ADD CONSTRAINT pk_asset_metrics PRIMARY KEY (id);


--
-- Name: asset_review_cycles pk_asset_review_cycles; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.asset_review_cycles
    ADD CONSTRAINT pk_asset_review_cycles PRIMARY KEY (id);


--
-- Name: asset_revisions pk_asset_revisions; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.asset_revisions
    ADD CONSTRAINT pk_asset_revisions PRIMARY KEY (id);


--
-- Name: asset_search_embeddings pk_asset_search_embeddings; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.asset_search_embeddings
    ADD CONSTRAINT pk_asset_search_embeddings PRIMARY KEY (id);


--
-- Name: assets pk_assets; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assets
    ADD CONSTRAINT pk_assets PRIMARY KEY (id);


--
-- Name: audit_access_tokens pk_audit_access_tokens; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audit_access_tokens
    ADD CONSTRAINT pk_audit_access_tokens PRIMARY KEY (id);


--
-- Name: audit_checkpoints pk_audit_checkpoints; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audit_checkpoints
    ADD CONSTRAINT pk_audit_checkpoints PRIMARY KEY (id);


--
-- Name: audit_events pk_audit_events; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audit_events
    ADD CONSTRAINT pk_audit_events PRIMARY KEY (id);


--
-- Name: auth_challenges pk_auth_challenges; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_challenges
    ADD CONSTRAINT pk_auth_challenges PRIMARY KEY (id);


--
-- Name: campaign_policy_versions pk_campaign_policy_versions; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.campaign_policy_versions
    ADD CONSTRAINT pk_campaign_policy_versions PRIMARY KEY (id);


--
-- Name: cleanup_campaigns pk_cleanup_campaigns; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cleanup_campaigns
    ADD CONSTRAINT pk_cleanup_campaigns PRIMARY KEY (id);


--
-- Name: collection_context_snapshots pk_collection_context_snapshots; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.collection_context_snapshots
    ADD CONSTRAINT pk_collection_context_snapshots PRIMARY KEY (id);


--
-- Name: collection_group_members pk_collection_group_members; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.collection_group_members
    ADD CONSTRAINT pk_collection_group_members PRIMARY KEY (id);


--
-- Name: collection_groups pk_collection_groups; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.collection_groups
    ADD CONSTRAINT pk_collection_groups PRIMARY KEY (id);


--
-- Name: deep_review_backfills pk_deep_review_backfills; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.deep_review_backfills
    ADD CONSTRAINT pk_deep_review_backfills PRIMARY KEY (id);


--
-- Name: deep_review_prompt_comparisons pk_deep_review_prompt_comparisons; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.deep_review_prompt_comparisons
    ADD CONSTRAINT pk_deep_review_prompt_comparisons PRIMARY KEY (id);


--
-- Name: deep_review_prompt_experiments pk_deep_review_prompt_experiments; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.deep_review_prompt_experiments
    ADD CONSTRAINT pk_deep_review_prompt_experiments PRIMARY KEY (id);


--
-- Name: deep_review_prompt_results pk_deep_review_prompt_results; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.deep_review_prompt_results
    ADD CONSTRAINT pk_deep_review_prompt_results PRIMARY KEY (id);


--
-- Name: ensemble_student_training_runs pk_ensemble_student_training_runs; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ensemble_student_training_runs
    ADD CONSTRAINT pk_ensemble_student_training_runs PRIMARY KEY (id);


--
-- Name: ensemble_student_versions pk_ensemble_student_versions; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ensemble_student_versions
    ADD CONSTRAINT pk_ensemble_student_versions PRIMARY KEY (id);


--
-- Name: human_decisions pk_human_decisions; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.human_decisions
    ADD CONSTRAINT pk_human_decisions PRIMARY KEY (id);


--
-- Name: immich_connections pk_immich_connections; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.immich_connections
    ADD CONSTRAINT pk_immich_connections PRIMARY KEY (id);


--
-- Name: immich_instances pk_immich_instances; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.immich_instances
    ADD CONSTRAINT pk_immich_instances PRIMARY KEY (id);


--
-- Name: inference_runs pk_inference_runs; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.inference_runs
    ADD CONSTRAINT pk_inference_runs PRIMARY KEY (id);


--
-- Name: library_policy_versions pk_library_policy_versions; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.library_policy_versions
    ADD CONSTRAINT pk_library_policy_versions PRIMARY KEY (id);


--
-- Name: library_programs pk_library_programs; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.library_programs
    ADD CONSTRAINT pk_library_programs PRIMARY KEY (id);


--
-- Name: library_ranking_runs pk_library_ranking_runs; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.library_ranking_runs
    ADD CONSTRAINT pk_library_ranking_runs PRIMARY KEY (id);


--
-- Name: library_run_schedules pk_library_run_schedules; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.library_run_schedules
    ADD CONSTRAINT pk_library_run_schedules PRIMARY KEY (id);


--
-- Name: model_deployments pk_model_deployments; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.model_deployments
    ADD CONSTRAINT pk_model_deployments PRIMARY KEY (id);


--
-- Name: model_packages pk_model_packages; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.model_packages
    ADD CONSTRAINT pk_model_packages PRIMARY KEY (id);


--
-- Name: model_versions pk_model_versions; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.model_versions
    ADD CONSTRAINT pk_model_versions PRIMARY KEY (id);


--
-- Name: normalized_derivatives pk_normalized_derivatives; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.normalized_derivatives
    ADD CONSTRAINT pk_normalized_derivatives PRIMARY KEY (id);


--
-- Name: operational_events pk_operational_events; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.operational_events
    ADD CONSTRAINT pk_operational_events PRIMARY KEY (id);


--
-- Name: owner_rating_session_items pk_owner_rating_session_items; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.owner_rating_session_items
    ADD CONSTRAINT pk_owner_rating_session_items PRIMARY KEY (id);


--
-- Name: owner_rating_sessions pk_owner_rating_sessions; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.owner_rating_sessions
    ADD CONSTRAINT pk_owner_rating_sessions PRIMARY KEY (id);


--
-- Name: passkey_credentials pk_passkey_credentials; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.passkey_credentials
    ADD CONSTRAINT pk_passkey_credentials PRIMARY KEY (id);


--
-- Name: person_references pk_person_references; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.person_references
    ADD CONSTRAINT pk_person_references PRIMARY KEY (id);


--
-- Name: personal_ranker_versions pk_personal_ranker_versions; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.personal_ranker_versions
    ADD CONSTRAINT pk_personal_ranker_versions PRIMARY KEY (id);


--
-- Name: preference_lora_adapters pk_preference_lora_adapters; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.preference_lora_adapters
    ADD CONSTRAINT pk_preference_lora_adapters PRIMARY KEY (id);


--
-- Name: preference_lora_runs pk_preference_lora_runs; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.preference_lora_runs
    ADD CONSTRAINT pk_preference_lora_runs PRIMARY KEY (id);


--
-- Name: preference_photo_insights pk_preference_photo_insights; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.preference_photo_insights
    ADD CONSTRAINT pk_preference_photo_insights PRIMARY KEY (id);


--
-- Name: preference_prompt_runs pk_preference_prompt_runs; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.preference_prompt_runs
    ADD CONSTRAINT pk_preference_prompt_runs PRIMARY KEY (id);


--
-- Name: processing_batches pk_processing_batches; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.processing_batches
    ADD CONSTRAINT pk_processing_batches PRIMARY KEY (id);


--
-- Name: proposals pk_proposals; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.proposals
    ADD CONSTRAINT pk_proposals PRIMARY KEY (id);


--
-- Name: repeatability_benchmarks pk_repeatability_benchmarks; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.repeatability_benchmarks
    ADD CONSTRAINT pk_repeatability_benchmarks PRIMARY KEY (id);


--
-- Name: resource_pressure_samples pk_resource_pressure_samples; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_pressure_samples
    ADD CONSTRAINT pk_resource_pressure_samples PRIMARY KEY (id);


--
-- Name: review_prompt_overrides pk_review_prompt_overrides; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.review_prompt_overrides
    ADD CONSTRAINT pk_review_prompt_overrides PRIMARY KEY (id);


--
-- Name: review_prompt_revisions pk_review_prompt_revisions; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.review_prompt_revisions
    ADD CONSTRAINT pk_review_prompt_revisions PRIMARY KEY (id);


--
-- Name: review_reanalysis_runs pk_review_reanalysis_runs; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.review_reanalysis_runs
    ADD CONSTRAINT pk_review_reanalysis_runs PRIMARY KEY (id);


--
-- Name: search_backfill_runs pk_search_backfill_runs; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.search_backfill_runs
    ADD CONSTRAINT pk_search_backfill_runs PRIMARY KEY (id);


--
-- Name: service_heartbeats pk_service_heartbeats; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.service_heartbeats
    ADD CONSTRAINT pk_service_heartbeats PRIMARY KEY (id);


--
-- Name: sessions pk_sessions; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sessions
    ADD CONSTRAINT pk_sessions PRIMARY KEY (id);


--
-- Name: similarity_group_members pk_similarity_group_members; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.similarity_group_members
    ADD CONSTRAINT pk_similarity_group_members PRIMARY KEY (id);


--
-- Name: similarity_groups pk_similarity_groups; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.similarity_groups
    ADD CONSTRAINT pk_similarity_groups PRIMARY KEY (id);


--
-- Name: storage_inventories pk_storage_inventories; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.storage_inventories
    ADD CONSTRAINT pk_storage_inventories PRIMARY KEY (scope);


--
-- Name: tag_references pk_tag_references; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tag_references
    ADD CONSTRAINT pk_tag_references PRIMARY KEY (id);


--
-- Name: task_failure_evidence pk_task_failure_evidence; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_failure_evidence
    ADD CONSTRAINT pk_task_failure_evidence PRIMARY KEY (id);


--
-- Name: tasks pk_tasks; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tasks
    ADD CONSTRAINT pk_tasks PRIMARY KEY (id);


--
-- Name: users pk_users; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT pk_users PRIMARY KEY (id);


--
-- Name: visual_embeddings pk_visual_embeddings; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.visual_embeddings
    ADD CONSTRAINT pk_visual_embeddings PRIMARY KEY (id);


--
-- Name: visual_fingerprint_bands pk_visual_fingerprint_bands; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.visual_fingerprint_bands
    ADD CONSTRAINT pk_visual_fingerprint_bands PRIMARY KEY (id);


--
-- Name: visual_fingerprints pk_visual_fingerprints; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.visual_fingerprints
    ADD CONSTRAINT pk_visual_fingerprints PRIMARY KEY (id);


--
-- Name: worker_credentials pk_worker_credentials; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.worker_credentials
    ADD CONSTRAINT pk_worker_credentials PRIMARY KEY (id);


--
-- Name: worker_enrollment_tokens pk_worker_enrollment_tokens; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.worker_enrollment_tokens
    ADD CONSTRAINT pk_worker_enrollment_tokens PRIMARY KEY (id);


--
-- Name: worker_model_availability pk_worker_model_availability; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.worker_model_availability
    ADD CONSTRAINT pk_worker_model_availability PRIMARY KEY (id);


--
-- Name: workers pk_workers; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workers
    ADD CONSTRAINT pk_workers PRIMARY KEY (id);


--
-- Name: workspace_presets pk_workspace_presets; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workspace_presets
    ADD CONSTRAINT pk_workspace_presets PRIMARY KEY (id);


--
-- Name: writeback_batches pk_writeback_batches; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.writeback_batches
    ADD CONSTRAINT pk_writeback_batches PRIMARY KEY (id);


--
-- Name: writeback_operations pk_writeback_operations; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.writeback_operations
    ADD CONSTRAINT pk_writeback_operations PRIMARY KEY (id);


--
-- Name: album_asset_references uq_album_asset_references_album_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.album_asset_references
    ADD CONSTRAINT uq_album_asset_references_album_id UNIQUE (album_id, asset_id);


--
-- Name: album_references uq_album_references_connection_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.album_references
    ADD CONSTRAINT uq_album_references_connection_id UNIQUE (connection_id, immich_album_id);


--
-- Name: analysis_generations uq_analysis_generation_asset_sequence; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.analysis_generations
    ADD CONSTRAINT uq_analysis_generation_asset_sequence UNIQUE (asset_id, asset_revision_id, sequence);


--
-- Name: analysis_nodes uq_analysis_node_identity; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.analysis_nodes
    ADD CONSTRAINT uq_analysis_node_identity UNIQUE (generation_id, stage_key, analyzer_version, inference_profile, input_digest);


--
-- Name: analysis_obligations uq_analysis_obligation_identity; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.analysis_obligations
    ADD CONSTRAINT uq_analysis_obligation_identity UNIQUE (asset_id, asset_revision_id, stage_key, processor_version, model_key);


--
-- Name: analysis_obligations uq_analysis_obligations_task_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.analysis_obligations
    ADD CONSTRAINT uq_analysis_obligations_task_id UNIQUE (task_id);


--
-- Name: analysis_runs uq_analysis_runs_generation_identity; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.analysis_runs
    ADD CONSTRAINT uq_analysis_runs_generation_identity UNIQUE (analysis_generation_id, asset_id, asset_revision_id, analysis_type, processor_version, model_key);


--
-- Name: api_credentials uq_api_credentials_connection_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.api_credentials
    ADD CONSTRAINT uq_api_credentials_connection_id UNIQUE (connection_id, kind, disabled_at);


--
-- Name: apple_vision_evidence uq_apple_vision_evidence_analysis_run_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.apple_vision_evidence
    ADD CONSTRAINT uq_apple_vision_evidence_analysis_run_id UNIQUE (analysis_run_id);


--
-- Name: asset_face_references uq_asset_face_references_connection_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.asset_face_references
    ADD CONSTRAINT uq_asset_face_references_connection_id UNIQUE (connection_id, immich_face_id);


--
-- Name: asset_metrics uq_asset_metrics_analysis_run_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.asset_metrics
    ADD CONSTRAINT uq_asset_metrics_analysis_run_id UNIQUE (analysis_run_id, name);


--
-- Name: asset_review_cycles uq_asset_review_cycle_identity; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.asset_review_cycles
    ADD CONSTRAINT uq_asset_review_cycle_identity UNIQUE (analysis_generation_id, asset_id, asset_revision_id, policy_version);


--
-- Name: asset_revisions uq_asset_revisions_asset_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.asset_revisions
    ADD CONSTRAINT uq_asset_revisions_asset_id UNIQUE (asset_id, metadata_digest);


--
-- Name: assets uq_assets_instance_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assets
    ADD CONSTRAINT uq_assets_instance_id UNIQUE (instance_id, connection_id, immich_asset_id);


--
-- Name: audit_checkpoints uq_audit_checkpoints_event_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audit_checkpoints
    ADD CONSTRAINT uq_audit_checkpoints_event_id UNIQUE (event_id);


--
-- Name: audit_checkpoints uq_audit_checkpoints_signature; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audit_checkpoints
    ADD CONSTRAINT uq_audit_checkpoints_signature UNIQUE (signature);


--
-- Name: audit_events uq_audit_events_event_hash; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audit_events
    ADD CONSTRAINT uq_audit_events_event_hash UNIQUE (event_hash);


--
-- Name: campaign_policy_versions uq_campaign_policy_versions_campaign_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.campaign_policy_versions
    ADD CONSTRAINT uq_campaign_policy_versions_campaign_id UNIQUE (campaign_id, version);


--
-- Name: collection_context_snapshots uq_collection_context_snapshot_identity; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.collection_context_snapshots
    ADD CONSTRAINT uq_collection_context_snapshot_identity UNIQUE (asset_id, asset_revision_id, processor_version);


--
-- Name: collection_groups uq_collection_group_identity; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.collection_groups
    ADD CONSTRAINT uq_collection_group_identity UNIQUE (group_type, group_key);


--
-- Name: collection_group_members uq_collection_group_members_group_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.collection_group_members
    ADD CONSTRAINT uq_collection_group_members_group_id UNIQUE (group_id, asset_id);


--
-- Name: deep_review_backfills uq_deep_review_backfills_idempotency_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.deep_review_backfills
    ADD CONSTRAINT uq_deep_review_backfills_idempotency_key UNIQUE (idempotency_key);


--
-- Name: deep_review_prompt_comparisons uq_deep_review_prompt_comparisons_experiment_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.deep_review_prompt_comparisons
    ADD CONSTRAINT uq_deep_review_prompt_comparisons_experiment_id UNIQUE (experiment_id, asset_id, actor_id);


--
-- Name: deep_review_prompt_results uq_deep_review_prompt_results_experiment_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.deep_review_prompt_results
    ADD CONSTRAINT uq_deep_review_prompt_results_experiment_id UNIQUE (experiment_id, asset_id, prompt_version);


--
-- Name: deep_review_prompt_results uq_deep_review_prompt_results_task_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.deep_review_prompt_results
    ADD CONSTRAINT uq_deep_review_prompt_results_task_id UNIQUE (task_id);


--
-- Name: ensemble_student_versions uq_ensemble_student_versions_artifact_digest; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ensemble_student_versions
    ADD CONSTRAINT uq_ensemble_student_versions_artifact_digest UNIQUE (artifact_digest);


--
-- Name: ensemble_student_versions uq_ensemble_student_versions_version; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ensemble_student_versions
    ADD CONSTRAINT uq_ensemble_student_versions_version UNIQUE (version);


--
-- Name: visual_embeddings uq_evidence_embedding_run_purpose; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.visual_embeddings
    ADD CONSTRAINT uq_evidence_embedding_run_purpose UNIQUE (analysis_run_id, purpose);


--
-- Name: human_decisions uq_human_decisions_idempotency_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.human_decisions
    ADD CONSTRAINT uq_human_decisions_idempotency_key UNIQUE (idempotency_key);


--
-- Name: immich_connections uq_immich_connections_instance_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.immich_connections
    ADD CONSTRAINT uq_immich_connections_instance_id UNIQUE (instance_id, immich_user_id);


--
-- Name: immich_instances uq_immich_instances_stable_instance_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.immich_instances
    ADD CONSTRAINT uq_immich_instances_stable_instance_id UNIQUE (stable_instance_id);


--
-- Name: inference_runs uq_inference_run_task_ordinal; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.inference_runs
    ADD CONSTRAINT uq_inference_run_task_ordinal UNIQUE (task_id, invocation_index);


--
-- Name: library_policy_versions uq_library_policy_versions_program_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.library_policy_versions
    ADD CONSTRAINT uq_library_policy_versions_program_id UNIQUE (program_id, version);


--
-- Name: library_programs uq_library_programs_name; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.library_programs
    ADD CONSTRAINT uq_library_programs_name UNIQUE (name);


--
-- Name: library_ranking_runs uq_library_ranking_runs_version; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.library_ranking_runs
    ADD CONSTRAINT uq_library_ranking_runs_version UNIQUE (version);


--
-- Name: library_run_schedules uq_library_run_schedules_program_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.library_run_schedules
    ADD CONSTRAINT uq_library_run_schedules_program_id UNIQUE (program_id);


--
-- Name: model_deployments uq_model_deployments_model_package_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.model_deployments
    ADD CONSTRAINT uq_model_deployments_model_package_id UNIQUE (model_package_id, worker_id, runtime);


--
-- Name: model_packages uq_model_packages_source_url; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.model_packages
    ADD CONSTRAINT uq_model_packages_source_url UNIQUE (source_url, source_revision, artifact_checksum);


--
-- Name: model_versions uq_model_versions_model_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.model_versions
    ADD CONSTRAINT uq_model_versions_model_id UNIQUE (model_id, version, runtime, quantization);


--
-- Name: normalized_derivatives uq_normalized_derivative_source_variant; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.normalized_derivatives
    ADD CONSTRAINT uq_normalized_derivative_source_variant UNIQUE (asset_id, asset_revision_id, variant, transform_version);


--
-- Name: operational_events uq_operational_events_event_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.operational_events
    ADD CONSTRAINT uq_operational_events_event_id UNIQUE (event_id);


--
-- Name: owner_rating_session_items uq_owner_rating_session_item_asset; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.owner_rating_session_items
    ADD CONSTRAINT uq_owner_rating_session_item_asset UNIQUE (session_id, asset_id);


--
-- Name: owner_rating_session_items uq_owner_rating_session_item_position; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.owner_rating_session_items
    ADD CONSTRAINT uq_owner_rating_session_item_position UNIQUE (session_id, "position");


--
-- Name: owner_rating_session_items uq_owner_rating_session_items_advance_idempotency_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.owner_rating_session_items
    ADD CONSTRAINT uq_owner_rating_session_items_advance_idempotency_key UNIQUE (advance_idempotency_key);


--
-- Name: owner_rating_sessions uq_owner_rating_sessions_idempotency_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.owner_rating_sessions
    ADD CONSTRAINT uq_owner_rating_sessions_idempotency_key UNIQUE (idempotency_key);


--
-- Name: passkey_credentials uq_passkey_credentials_credential_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.passkey_credentials
    ADD CONSTRAINT uq_passkey_credentials_credential_id UNIQUE (credential_id);


--
-- Name: person_references uq_person_references_connection_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.person_references
    ADD CONSTRAINT uq_person_references_connection_id UNIQUE (connection_id, immich_person_id);


--
-- Name: personal_ranker_versions uq_personal_ranker_versions_artifact_digest; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.personal_ranker_versions
    ADD CONSTRAINT uq_personal_ranker_versions_artifact_digest UNIQUE (artifact_digest);


--
-- Name: personal_ranker_versions uq_personal_ranker_versions_version; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.personal_ranker_versions
    ADD CONSTRAINT uq_personal_ranker_versions_version UNIQUE (version);


--
-- Name: preference_lora_adapters uq_preference_lora_adapter_version; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.preference_lora_adapters
    ADD CONSTRAINT uq_preference_lora_adapter_version UNIQUE (version);


--
-- Name: preference_lora_adapters uq_preference_lora_adapters_artifact_digest; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.preference_lora_adapters
    ADD CONSTRAINT uq_preference_lora_adapters_artifact_digest UNIQUE (artifact_digest);


--
-- Name: preference_lora_adapters uq_preference_lora_adapters_run_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.preference_lora_adapters
    ADD CONSTRAINT uq_preference_lora_adapters_run_id UNIQUE (run_id);


--
-- Name: preference_lora_runs uq_preference_lora_runs_idempotency_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.preference_lora_runs
    ADD CONSTRAINT uq_preference_lora_runs_idempotency_key UNIQUE (idempotency_key);


--
-- Name: preference_lora_runs uq_preference_lora_runs_task_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.preference_lora_runs
    ADD CONSTRAINT uq_preference_lora_runs_task_id UNIQUE (task_id);


--
-- Name: preference_photo_insights uq_preference_photo_insight_asset; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.preference_photo_insights
    ADD CONSTRAINT uq_preference_photo_insight_asset UNIQUE (run_id, asset_id);


--
-- Name: preference_photo_insights uq_preference_photo_insights_task_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.preference_photo_insights
    ADD CONSTRAINT uq_preference_photo_insights_task_id UNIQUE (task_id);


--
-- Name: preference_prompt_runs uq_preference_prompt_runs_idempotency_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.preference_prompt_runs
    ADD CONSTRAINT uq_preference_prompt_runs_idempotency_key UNIQUE (idempotency_key);


--
-- Name: preference_prompt_runs uq_preference_prompt_runs_proposed_revision_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.preference_prompt_runs
    ADD CONSTRAINT uq_preference_prompt_runs_proposed_revision_id UNIQUE (proposed_revision_id);


--
-- Name: preference_prompt_runs uq_preference_prompt_runs_synthesis_task_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.preference_prompt_runs
    ADD CONSTRAINT uq_preference_prompt_runs_synthesis_task_id UNIQUE (synthesis_task_id);


--
-- Name: analysis_publication_snapshots uq_pub_snap_generation; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.analysis_publication_snapshots
    ADD CONSTRAINT uq_pub_snap_generation UNIQUE (analysis_generation_id);


--
-- Name: review_prompt_overrides uq_review_prompt_overrides_prompt_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.review_prompt_overrides
    ADD CONSTRAINT uq_review_prompt_overrides_prompt_key UNIQUE (prompt_key);


--
-- Name: review_prompt_revisions uq_review_prompt_revision_version; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.review_prompt_revisions
    ADD CONSTRAINT uq_review_prompt_revision_version UNIQUE (prompt_key, version);


--
-- Name: search_backfill_runs uq_search_backfill_runs_idempotency_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.search_backfill_runs
    ADD CONSTRAINT uq_search_backfill_runs_idempotency_key UNIQUE (idempotency_key);


--
-- Name: asset_search_embeddings uq_search_embedding_run_purpose; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.asset_search_embeddings
    ADD CONSTRAINT uq_search_embedding_run_purpose UNIQUE (analysis_run_id, purpose);


--
-- Name: service_heartbeats uq_service_heartbeats_service_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.service_heartbeats
    ADD CONSTRAINT uq_service_heartbeats_service_key UNIQUE (service_key);


--
-- Name: sessions uq_sessions_token_hash; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sessions
    ADD CONSTRAINT uq_sessions_token_hash UNIQUE (token_hash);


--
-- Name: similarity_group_members uq_similarity_group_members_group_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.similarity_group_members
    ADD CONSTRAINT uq_similarity_group_members_group_id UNIQUE (group_id, asset_id);


--
-- Name: similarity_groups uq_similarity_groups_campaign_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.similarity_groups
    ADD CONSTRAINT uq_similarity_groups_campaign_id UNIQUE (campaign_id, group_key);


--
-- Name: tag_references uq_tag_references_connection_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tag_references
    ADD CONSTRAINT uq_tag_references_connection_id UNIQUE (connection_id, immich_tag_id);


--
-- Name: task_failure_evidence uq_task_failure_evidence_attempt; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_failure_evidence
    ADD CONSTRAINT uq_task_failure_evidence_attempt UNIQUE (task_id, attempt_number, error_class);


--
-- Name: tasks uq_tasks_idempotency_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tasks
    ADD CONSTRAINT uq_tasks_idempotency_key UNIQUE (idempotency_key);


--
-- Name: tasks uq_tasks_lease_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tasks
    ADD CONSTRAINT uq_tasks_lease_id UNIQUE (lease_id);


--
-- Name: tasks uq_tasks_temporal_workflow_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tasks
    ADD CONSTRAINT uq_tasks_temporal_workflow_id UNIQUE (temporal_workflow_id);


--
-- Name: users uq_users_username; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT uq_users_username UNIQUE (username);


--
-- Name: visual_fingerprint_bands uq_visual_fingerprint_bands_fingerprint_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.visual_fingerprint_bands
    ADD CONSTRAINT uq_visual_fingerprint_bands_fingerprint_id UNIQUE (fingerprint_id, band_index);


--
-- Name: visual_fingerprints uq_visual_fingerprints_asset_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.visual_fingerprints
    ADD CONSTRAINT uq_visual_fingerprints_asset_id UNIQUE (asset_id, asset_revision_id, algorithm, algorithm_version);


--
-- Name: worker_credentials uq_worker_credentials_token_hash; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.worker_credentials
    ADD CONSTRAINT uq_worker_credentials_token_hash UNIQUE (token_hash);


--
-- Name: worker_enrollment_tokens uq_worker_enrollment_tokens_token_hash; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.worker_enrollment_tokens
    ADD CONSTRAINT uq_worker_enrollment_tokens_token_hash UNIQUE (token_hash);


--
-- Name: worker_model_availability uq_worker_model_availability_worker_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.worker_model_availability
    ADD CONSTRAINT uq_worker_model_availability_worker_id UNIQUE (worker_id, model_version_id);


--
-- Name: workers uq_workers_certificate_subject; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workers
    ADD CONSTRAINT uq_workers_certificate_subject UNIQUE (certificate_subject);


--
-- Name: workers uq_workers_hostname_label; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workers
    ADD CONSTRAINT uq_workers_hostname_label UNIQUE (hostname_label);


--
-- Name: workers uq_workers_token_hash; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workers
    ADD CONSTRAINT uq_workers_token_hash UNIQUE (token_hash);


--
-- Name: workspace_presets uq_workspace_preset_owner_kind_name; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workspace_presets
    ADD CONSTRAINT uq_workspace_preset_owner_kind_name UNIQUE (owner_id, kind, name);


--
-- Name: writeback_operations uq_writeback_operations_batch_asset_operation; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.writeback_operations
    ADD CONSTRAINT uq_writeback_operations_batch_asset_operation UNIQUE (batch_id, asset_id, operation_type);


--
-- Name: writeback_operations uq_writeback_operations_idempotency_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.writeback_operations
    ADD CONSTRAINT uq_writeback_operations_idempotency_key UNIQUE (idempotency_key);


--
-- Name: ix_album_asset_references_album_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_album_asset_references_album_id ON public.album_asset_references USING btree (album_id);


--
-- Name: ix_album_asset_references_asset_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_album_asset_references_asset_id ON public.album_asset_references USING btree (asset_id);


--
-- Name: ix_album_references_connection_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_album_references_connection_id ON public.album_references USING btree (connection_id);


--
-- Name: ix_analysis_campaign_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_analysis_campaign_status ON public.analysis_runs USING btree (campaign_id, status);


--
-- Name: ix_analysis_generations_asset_revision_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_analysis_generations_asset_revision_id ON public.analysis_generations USING btree (asset_revision_id);


--
-- Name: ix_analysis_generations_asset_state; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_analysis_generations_asset_state ON public.analysis_generations USING btree (asset_id, state);


--
-- Name: ix_analysis_generations_parent_generation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_analysis_generations_parent_generation_id ON public.analysis_generations USING btree (parent_generation_id);


--
-- Name: ix_analysis_generations_personal_ranker_version; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_analysis_generations_personal_ranker_version ON public.analysis_generations USING btree (personal_ranker_version);


--
-- Name: ix_analysis_generations_personal_ranker_version_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_analysis_generations_personal_ranker_version_id ON public.analysis_generations USING btree (personal_ranker_version_id);


--
-- Name: ix_analysis_generations_state_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_analysis_generations_state_created ON public.analysis_generations USING btree (state, created_at);


--
-- Name: ix_analysis_nodes_asset_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_analysis_nodes_asset_id ON public.analysis_nodes USING btree (asset_id);


--
-- Name: ix_analysis_nodes_generation_state; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_analysis_nodes_generation_state ON public.analysis_nodes USING btree (generation_id, state);


--
-- Name: ix_analysis_nodes_stage_state_completed; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_analysis_nodes_stage_state_completed ON public.analysis_nodes USING btree (stage_key, state, completed_at, generation_id);


--
-- Name: ix_analysis_nodes_task; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_analysis_nodes_task ON public.analysis_nodes USING btree (task_id);


--
-- Name: ix_analysis_obligations_asset_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_analysis_obligations_asset_id ON public.analysis_obligations USING btree (asset_id);


--
-- Name: ix_analysis_obligations_asset_revision_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_analysis_obligations_asset_revision_id ON public.analysis_obligations USING btree (asset_revision_id);


--
-- Name: ix_analysis_obligations_asset_state; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_analysis_obligations_asset_state ON public.analysis_obligations USING btree (asset_id, state);


--
-- Name: ix_analysis_obligations_batch_state; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_analysis_obligations_batch_state ON public.analysis_obligations USING btree (processing_batch_id, state);


--
-- Name: ix_analysis_obligations_evidence_digest; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_analysis_obligations_evidence_digest ON public.analysis_obligations USING btree (evidence_digest);


--
-- Name: ix_analysis_obligations_library_program_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_analysis_obligations_library_program_id ON public.analysis_obligations USING btree (library_program_id);


--
-- Name: ix_analysis_obligations_processing_batch_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_analysis_obligations_processing_batch_id ON public.analysis_obligations USING btree (processing_batch_id);


--
-- Name: ix_analysis_obligations_review_cycle_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_analysis_obligations_review_cycle_id ON public.analysis_obligations USING btree (review_cycle_id);


--
-- Name: ix_analysis_obligations_runnable; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_analysis_obligations_runnable ON public.analysis_obligations USING btree (state, completion_role, priority_tier, priority_score, created_at);


--
-- Name: ix_analysis_publication_snapshots_asset_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_analysis_publication_snapshots_asset_id ON public.analysis_publication_snapshots USING btree (asset_id);


--
-- Name: ix_analysis_runs_analysis_generation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_analysis_runs_analysis_generation_id ON public.analysis_runs USING btree (analysis_generation_id);


--
-- Name: ix_analysis_runs_asset_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_analysis_runs_asset_id ON public.analysis_runs USING btree (asset_id);


--
-- Name: ix_analysis_runs_campaign_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_analysis_runs_campaign_id ON public.analysis_runs USING btree (campaign_id);


--
-- Name: ix_analysis_runs_library_program_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_analysis_runs_library_program_id ON public.analysis_runs USING btree (library_program_id);


--
-- Name: ix_api_credentials_connection_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_api_credentials_connection_id ON public.api_credentials USING btree (connection_id);


--
-- Name: ix_apple_vision_evidence_asset_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_apple_vision_evidence_asset_created ON public.apple_vision_evidence USING btree (asset_id, created_at);


--
-- Name: ix_apple_vision_evidence_asset_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_apple_vision_evidence_asset_id ON public.apple_vision_evidence USING btree (asset_id);


--
-- Name: ix_apple_vision_evidence_asset_revision_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_apple_vision_evidence_asset_revision_id ON public.apple_vision_evidence USING btree (asset_revision_id);


--
-- Name: ix_asset_curation_complete; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_asset_curation_complete ON public.asset_curation_snapshots USING btree (analysis_complete, asset_id);


--
-- Name: ix_asset_curation_personal; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_asset_curation_personal ON public.asset_curation_snapshots USING btree (personal_score, asset_id);


--
-- Name: ix_asset_curation_rank; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_asset_curation_rank ON public.asset_curation_snapshots USING btree (rank_score, asset_id);


--
-- Name: ix_asset_curation_ranking_state; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_asset_curation_ranking_state ON public.asset_curation_snapshots USING btree (ranking_state, library_position);


--
-- Name: ix_asset_curation_rating; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_asset_curation_rating ON public.asset_curation_snapshots USING btree (effective_rating, rank_score);


--
-- Name: ix_asset_curation_review; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_asset_curation_review ON public.asset_curation_snapshots USING btree (review_priority, asset_id);


--
-- Name: ix_asset_curation_snapshots_asset_revision_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_asset_curation_snapshots_asset_revision_id ON public.asset_curation_snapshots USING btree (asset_revision_id);


--
-- Name: ix_asset_curation_snapshots_evidence_digest; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_asset_curation_snapshots_evidence_digest ON public.asset_curation_snapshots USING btree (evidence_digest);


--
-- Name: ix_asset_curation_snapshots_personal_model_version_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_asset_curation_snapshots_personal_model_version_id ON public.asset_curation_snapshots USING btree (personal_model_version_id);


--
-- Name: ix_asset_curation_snapshots_student_model_version_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_asset_curation_snapshots_student_model_version_id ON public.asset_curation_snapshots USING btree (student_model_version_id);


--
-- Name: ix_asset_curation_student; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_asset_curation_student ON public.asset_curation_snapshots USING btree (student_score, asset_id);


--
-- Name: ix_asset_face_references_asset_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_asset_face_references_asset_id ON public.asset_face_references USING btree (asset_id);


--
-- Name: ix_asset_face_references_connection_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_asset_face_references_connection_id ON public.asset_face_references USING btree (connection_id);


--
-- Name: ix_asset_face_references_immich_person_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_asset_face_references_immich_person_id ON public.asset_face_references USING btree (immich_person_id);


--
-- Name: ix_asset_immich_contexts_fetched_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_asset_immich_contexts_fetched_at ON public.asset_immich_contexts USING btree (fetched_at);


--
-- Name: ix_asset_metrics_analysis_run_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_asset_metrics_analysis_run_id ON public.asset_metrics USING btree (analysis_run_id);


--
-- Name: ix_asset_ml_scores_personal; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_asset_ml_scores_personal ON public.asset_ml_scores USING btree (personal_score, asset_id);


--
-- Name: ix_asset_ml_scores_personal_model_version_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_asset_ml_scores_personal_model_version_id ON public.asset_ml_scores USING btree (personal_model_version_id);


--
-- Name: ix_asset_ml_scores_student; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_asset_ml_scores_student ON public.asset_ml_scores USING btree (student_score, asset_id);


--
-- Name: ix_asset_ml_scores_student_model_version_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_asset_ml_scores_student_model_version_id ON public.asset_ml_scores USING btree (student_model_version_id);


--
-- Name: ix_asset_pipeline_state_asset_revision_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_asset_pipeline_state_asset_revision_id ON public.asset_pipeline_state USING btree (asset_revision_id);


--
-- Name: ix_asset_pipeline_state_complete; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_asset_pipeline_state_complete ON public.asset_pipeline_state USING btree (analysis_complete, asset_id);


--
-- Name: ix_asset_pipeline_state_evidence_digest; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_asset_pipeline_state_evidence_digest ON public.asset_pipeline_state USING btree (evidence_digest);


--
-- Name: ix_asset_ratings_rank; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_asset_ratings_rank ON public.asset_ratings USING btree (rank_score, asset_id);


--
-- Name: ix_asset_ratings_rating; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_asset_ratings_rating ON public.asset_ratings USING btree (effective_rating, rank_score);


--
-- Name: ix_asset_ratings_review; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_asset_ratings_review ON public.asset_ratings USING btree (review_priority, asset_id);


--
-- Name: ix_asset_review_cycles_analysis_generation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_asset_review_cycles_analysis_generation_id ON public.asset_review_cycles USING btree (analysis_generation_id);


--
-- Name: ix_asset_review_cycles_asset_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_asset_review_cycles_asset_id ON public.asset_review_cycles USING btree (asset_id);


--
-- Name: ix_asset_review_cycles_asset_revision_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_asset_review_cycles_asset_revision_id ON public.asset_review_cycles USING btree (asset_revision_id);


--
-- Name: ix_asset_review_cycles_batch_state; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_asset_review_cycles_batch_state ON public.asset_review_cycles USING btree (processing_batch_id, state);


--
-- Name: ix_asset_review_cycles_library_program_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_asset_review_cycles_library_program_id ON public.asset_review_cycles USING btree (library_program_id);


--
-- Name: ix_asset_review_cycles_processing_batch_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_asset_review_cycles_processing_batch_id ON public.asset_review_cycles USING btree (processing_batch_id);


--
-- Name: ix_asset_review_cycles_state; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_asset_review_cycles_state ON public.asset_review_cycles USING btree (state, updated_at);


--
-- Name: ix_asset_revisions_asset_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_asset_revisions_asset_id ON public.asset_revisions USING btree (asset_id);


--
-- Name: ix_asset_revisions_gallery_camera; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_asset_revisions_gallery_camera ON public.asset_revisions USING btree (btrim(((COALESCE((normalized_metadata #>> '{exifInfo,make}'::text[]), ''::text) || ' '::text) || COALESCE((normalized_metadata #>> '{exifInfo,model}'::text[]), ''::text))));


--
-- Name: ix_asset_revisions_gallery_captured; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_asset_revisions_gallery_captured ON public.asset_revisions USING btree (COALESCE((normalized_metadata ->> 'localDateTime'::text), (normalized_metadata ->> 'fileCreatedAt'::text), (normalized_metadata ->> 'dateTimeOriginal'::text), ''::text));


--
-- Name: ix_asset_search_embeddings_analysis_generation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_asset_search_embeddings_analysis_generation_id ON public.asset_search_embeddings USING btree (analysis_generation_id);


--
-- Name: ix_assets_access_state_excluded_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_assets_access_state_excluded_at ON public.assets USING btree (access_state, excluded_at);


--
-- Name: ix_assets_analysis_generation; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_assets_analysis_generation ON public.assets USING btree (analysis_generation);


--
-- Name: ix_assets_connection_last_seen; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_assets_connection_last_seen ON public.assets USING btree (connection_id, last_seen_at);


--
-- Name: ix_assets_current_generation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_assets_current_generation_id ON public.assets USING btree (current_generation_id);


--
-- Name: ix_assets_gallery_cache_priority; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_assets_gallery_cache_priority ON public.assets USING btree (created_at DESC, id DESC);


--
-- Name: ix_assets_media_access_revision; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_assets_media_access_revision ON public.assets USING btree (media_type, access_state, current_revision_id);


--
-- Name: ix_audit_access_tokens_owner_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_audit_access_tokens_owner_id ON public.audit_access_tokens USING btree (owner_id);


--
-- Name: ix_audit_created_correlation; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_audit_created_correlation ON public.audit_events USING btree (created_at, correlation_id);


--
-- Name: ix_audit_token_hash; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_audit_token_hash ON public.audit_access_tokens USING btree (token_hash);


--
-- Name: ix_cleanup_campaigns_library_program_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_cleanup_campaigns_library_program_id ON public.cleanup_campaigns USING btree (library_program_id);


--
-- Name: ix_collection_context_asset_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_collection_context_asset_status ON public.collection_context_snapshots USING btree (asset_id, status);


--
-- Name: ix_collection_context_snapshots_asset_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_collection_context_snapshots_asset_id ON public.collection_context_snapshots USING btree (asset_id);


--
-- Name: ix_collection_context_snapshots_asset_revision_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_collection_context_snapshots_asset_revision_id ON public.collection_context_snapshots USING btree (asset_revision_id);


--
-- Name: ix_collection_context_snapshots_evidence_digest; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_collection_context_snapshots_evidence_digest ON public.collection_context_snapshots USING btree (evidence_digest);


--
-- Name: ix_collection_group_members_asset; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_collection_group_members_asset ON public.collection_group_members USING btree (asset_id, group_id);


--
-- Name: ix_collection_group_members_asset_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_collection_group_members_asset_id ON public.collection_group_members USING btree (asset_id);


--
-- Name: ix_collection_group_members_group_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_collection_group_members_group_id ON public.collection_group_members USING btree (group_id);


--
-- Name: ix_collection_groups_connection_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_collection_groups_connection_id ON public.collection_groups USING btree (connection_id);


--
-- Name: ix_collection_groups_representative_asset_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_collection_groups_representative_asset_id ON public.collection_groups USING btree (representative_asset_id);


--
-- Name: ix_collection_groups_type_state; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_collection_groups_type_state ON public.collection_groups USING btree (group_type, state);


--
-- Name: ix_connection_health_last_sync; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_connection_health_last_sync ON public.immich_connections USING btree (health, last_sync_at);


--
-- Name: ix_deep_review_backfills_program_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_deep_review_backfills_program_id ON public.deep_review_backfills USING btree (program_id);


--
-- Name: ix_deep_review_backfills_state_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_deep_review_backfills_state_created ON public.deep_review_backfills USING btree (state, created_at);


--
-- Name: ix_deep_review_prompt_comparisons_experiment_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_deep_review_prompt_comparisons_experiment_id ON public.deep_review_prompt_comparisons USING btree (experiment_id);


--
-- Name: ix_deep_review_prompt_experiments_model_version_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_deep_review_prompt_experiments_model_version_id ON public.deep_review_prompt_experiments USING btree (model_version_id);


--
-- Name: ix_deep_review_prompt_results_asset_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_deep_review_prompt_results_asset_id ON public.deep_review_prompt_results USING btree (asset_id);


--
-- Name: ix_deep_review_prompt_results_experiment_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_deep_review_prompt_results_experiment_id ON public.deep_review_prompt_results USING btree (experiment_id);


--
-- Name: ix_deep_review_prompt_results_experiment_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_deep_review_prompt_results_experiment_status ON public.deep_review_prompt_results USING btree (experiment_id, status);


--
-- Name: ix_ensemble_student_training_runs_model_version_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_ensemble_student_training_runs_model_version_id ON public.ensemble_student_training_runs USING btree (model_version_id);


--
-- Name: ix_ensemble_student_training_runs_requested_by; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_ensemble_student_training_runs_requested_by ON public.ensemble_student_training_runs USING btree (requested_by);


--
-- Name: ix_ensemble_student_training_runs_started_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_ensemble_student_training_runs_started_at ON public.ensemble_student_training_runs USING btree (started_at);


--
-- Name: ix_ensemble_student_training_runs_state; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_ensemble_student_training_runs_state ON public.ensemble_student_training_runs USING btree (state);


--
-- Name: ix_ensemble_student_training_runs_trigger; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_ensemble_student_training_runs_trigger ON public.ensemble_student_training_runs USING btree (trigger);


--
-- Name: ix_ensemble_student_versions_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_ensemble_student_versions_status ON public.ensemble_student_versions USING btree (status);


--
-- Name: ix_ensemble_student_versions_training_data_digest; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_ensemble_student_versions_training_data_digest ON public.ensemble_student_versions USING btree (training_data_digest);


--
-- Name: ix_fingerprint_algorithm_value; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_fingerprint_algorithm_value ON public.visual_fingerprints USING btree (algorithm, value);


--
-- Name: ix_highlight_comparisons_generation_state; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_highlight_comparisons_generation_state ON public.highlight_comparisons USING btree (generation, state);


--
-- Name: ix_highlight_league_entries_analysis_generation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_highlight_league_entries_analysis_generation_id ON public.highlight_league_entries USING btree (analysis_generation_id);


--
-- Name: ix_highlight_league_entries_asset_revision_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_highlight_league_entries_asset_revision_id ON public.highlight_league_entries USING btree (asset_revision_id);


--
-- Name: ix_highlight_league_entries_campaign_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_highlight_league_entries_campaign_id ON public.highlight_league_entries USING btree (campaign_id);


--
-- Name: ix_highlight_league_five_star_batch; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_highlight_league_five_star_batch ON public.highlight_league_entries USING btree (five_star_batch, five_star_state);


--
-- Name: ix_highlight_league_generation_score; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_highlight_league_generation_score ON public.highlight_league_entries USING btree (generation, conservative_score DESC);


--
-- Name: ix_highlight_league_relative_proposal; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_highlight_league_relative_proposal ON public.highlight_league_entries USING btree (relative_rating_proposal_id);


--
-- Name: ix_highlight_league_state_matches; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_highlight_league_state_matches ON public.highlight_league_entries USING btree (state, match_count);


--
-- Name: ix_human_decisions_campaign_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_human_decisions_campaign_id ON public.human_decisions USING btree (campaign_id);


--
-- Name: ix_human_decisions_target_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_human_decisions_target_id ON public.human_decisions USING btree (target_id);


--
-- Name: ix_immich_connections_sync_lease_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_immich_connections_sync_lease_id ON public.immich_connections USING btree (sync_lease_id);


--
-- Name: ix_inference_runs_analysis_run_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_inference_runs_analysis_run_id ON public.inference_runs USING btree (analysis_run_id);


--
-- Name: ix_inference_runs_asset_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_inference_runs_asset_id ON public.inference_runs USING btree (asset_id);


--
-- Name: ix_inference_runs_generation; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_inference_runs_generation ON public.inference_runs USING btree (analysis_generation_id);


--
-- Name: ix_inference_runs_model_completed; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_inference_runs_model_completed ON public.inference_runs USING btree (model_key, completed_at);


--
-- Name: ix_inference_runs_task_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_inference_runs_task_id ON public.inference_runs USING btree (task_id);


--
-- Name: ix_library_policy_versions_program_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_library_policy_versions_program_id ON public.library_policy_versions USING btree (program_id);


--
-- Name: ix_library_run_schedules_next_due_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_library_run_schedules_next_due_at ON public.library_run_schedules USING btree (next_due_at);


--
-- Name: ix_library_run_schedules_program_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_library_run_schedules_program_id ON public.library_run_schedules USING btree (program_id);


--
-- Name: ix_model_deployments_model_package_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_model_deployments_model_package_id ON public.model_deployments USING btree (model_package_id);


--
-- Name: ix_model_deployments_worker_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_model_deployments_worker_id ON public.model_deployments USING btree (worker_id);


--
-- Name: ix_model_packages_model_version_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_model_packages_model_version_id ON public.model_packages USING btree (model_version_id);


--
-- Name: ix_normalized_derivatives_asset_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_normalized_derivatives_asset_id ON public.normalized_derivatives USING btree (asset_id);


--
-- Name: ix_normalized_derivatives_asset_revision_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_normalized_derivatives_asset_revision_id ON public.normalized_derivatives USING btree (asset_revision_id);


--
-- Name: ix_normalized_derivatives_digest; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_normalized_derivatives_digest ON public.normalized_derivatives USING btree (content_digest);


--
-- Name: ix_operational_events_connection_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_operational_events_connection_id ON public.operational_events USING btree (connection_id);


--
-- Name: ix_operational_events_created_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_operational_events_created_id ON public.operational_events USING btree (created_at, id);


--
-- Name: ix_operational_events_model_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_operational_events_model_id ON public.operational_events USING btree (model_id);


--
-- Name: ix_operational_events_source_severity_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_operational_events_source_severity_id ON public.operational_events USING btree (source, severity, id);


--
-- Name: ix_operational_events_task_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_operational_events_task_id ON public.operational_events USING btree (task_id);


--
-- Name: ix_operational_events_task_id_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_operational_events_task_id_id ON public.operational_events USING btree (task_id, id);


--
-- Name: ix_operational_events_worker_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_operational_events_worker_id ON public.operational_events USING btree (worker_id);


--
-- Name: ix_operational_events_worker_id_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_operational_events_worker_id_id ON public.operational_events USING btree (worker_id, id);


--
-- Name: ix_owner_rating_session_items_asset_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_owner_rating_session_items_asset_id ON public.owner_rating_session_items USING btree (asset_id);


--
-- Name: ix_owner_rating_session_items_session_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_owner_rating_session_items_session_id ON public.owner_rating_session_items USING btree (session_id);


--
-- Name: ix_owner_rating_session_items_session_state; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_owner_rating_session_items_session_state ON public.owner_rating_session_items USING btree (session_id, state);


--
-- Name: ix_owner_rating_sessions_owner_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_owner_rating_sessions_owner_id ON public.owner_rating_sessions USING btree (owner_id);


--
-- Name: ix_owner_rating_sessions_owner_state; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_owner_rating_sessions_owner_state ON public.owner_rating_sessions USING btree (owner_id, state, created_at);


--
-- Name: ix_passkey_credentials_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_passkey_credentials_user_id ON public.passkey_credentials USING btree (user_id);


--
-- Name: ix_person_references_connection_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_person_references_connection_id ON public.person_references USING btree (connection_id);


--
-- Name: ix_personal_ranker_versions_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_personal_ranker_versions_status ON public.personal_ranker_versions USING btree (status);


--
-- Name: ix_personal_ranker_versions_training_data_digest; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_personal_ranker_versions_training_data_digest ON public.personal_ranker_versions USING btree (training_data_digest);


--
-- Name: ix_preference_lora_adapters_status_version; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_preference_lora_adapters_status_version ON public.preference_lora_adapters USING btree (status, version);


--
-- Name: ix_preference_lora_runs_dataset_digest; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_preference_lora_runs_dataset_digest ON public.preference_lora_runs USING btree (dataset_digest);


--
-- Name: ix_preference_lora_runs_state_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_preference_lora_runs_state_created ON public.preference_lora_runs USING btree (state, created_at);


--
-- Name: ix_preference_photo_insights_analysis_run_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_preference_photo_insights_analysis_run_id ON public.preference_photo_insights USING btree (analysis_run_id);


--
-- Name: ix_preference_photo_insights_asset_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_preference_photo_insights_asset_id ON public.preference_photo_insights USING btree (asset_id);


--
-- Name: ix_preference_photo_insights_automatic_review_digest; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_preference_photo_insights_automatic_review_digest ON public.preference_photo_insights USING btree (automatic_review_digest);


--
-- Name: ix_preference_photo_insights_owner_decision_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_preference_photo_insights_owner_decision_id ON public.preference_photo_insights USING btree (owner_decision_id);


--
-- Name: ix_preference_photo_insights_run_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_preference_photo_insights_run_id ON public.preference_photo_insights USING btree (run_id);


--
-- Name: ix_preference_photo_insights_run_state; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_preference_photo_insights_run_state ON public.preference_photo_insights USING btree (run_id, state);


--
-- Name: ix_preference_prompt_runs_personal_ranker_version_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_preference_prompt_runs_personal_ranker_version_id ON public.preference_prompt_runs USING btree (personal_ranker_version_id);


--
-- Name: ix_preference_prompt_runs_state; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_preference_prompt_runs_state ON public.preference_prompt_runs USING btree (state, created_at);


--
-- Name: ix_pressure_sampled_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pressure_sampled_at ON public.resource_pressure_samples USING btree (sampled_at);


--
-- Name: ix_processing_batches_campaign_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_processing_batches_campaign_id ON public.processing_batches USING btree (campaign_id);


--
-- Name: ix_processing_batches_program_state; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_processing_batches_program_state ON public.processing_batches USING btree (program_id, state);


--
-- Name: ix_proposals_analysis_generation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_proposals_analysis_generation_id ON public.proposals USING btree (analysis_generation_id);


--
-- Name: ix_proposals_authority_stage; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_proposals_authority_stage ON public.proposals USING btree (authority_stage);


--
-- Name: ix_proposals_campaign_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_proposals_campaign_id ON public.proposals USING btree (campaign_id);


--
-- Name: ix_proposals_target_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_proposals_target_id ON public.proposals USING btree (target_id);


--
-- Name: ix_repeatability_model_task; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_repeatability_model_task ON public.repeatability_benchmarks USING btree (model_key, task, created_at);


--
-- Name: ix_review_prompt_revision_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_review_prompt_revision_status ON public.review_prompt_revisions USING btree (prompt_key, status, version);


--
-- Name: ix_review_prompt_revisions_personal_ranker_version_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_review_prompt_revisions_personal_ranker_version_id ON public.review_prompt_revisions USING btree (personal_ranker_version_id);


--
-- Name: ix_review_reanalysis_runs_program_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_review_reanalysis_runs_program_id ON public.review_reanalysis_runs USING btree (program_id);


--
-- Name: ix_review_reanalysis_runs_request_digest; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_review_reanalysis_runs_request_digest ON public.review_reanalysis_runs USING btree (request_digest);


--
-- Name: ix_search_backfill_runs_state_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_search_backfill_runs_state_created ON public.search_backfill_runs USING btree (state, created_at);


--
-- Name: ix_search_embedding_asset; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_search_embedding_asset ON public.asset_search_embeddings USING btree (asset_id);


--
-- Name: ix_search_embedding_hnsw; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_search_embedding_hnsw ON public.asset_search_embeddings USING hnsw (embedding public.halfvec_cosine_ops);


--
-- Name: ix_search_embedding_purpose_version; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_search_embedding_purpose_version ON public.asset_search_embeddings USING btree (purpose, algorithm_version);


--
-- Name: ix_service_heartbeats_last_heartbeat; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_service_heartbeats_last_heartbeat ON public.service_heartbeats USING btree (last_heartbeat);


--
-- Name: ix_sessions_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_sessions_user_id ON public.sessions USING btree (user_id);


--
-- Name: ix_similarity_group_members_asset_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_similarity_group_members_asset_id ON public.similarity_group_members USING btree (asset_id);


--
-- Name: ix_similarity_group_members_group_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_similarity_group_members_group_id ON public.similarity_group_members USING btree (group_id);


--
-- Name: ix_similarity_groups_campaign_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_similarity_groups_campaign_id ON public.similarity_groups USING btree (campaign_id);


--
-- Name: ix_similarity_groups_library_program_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_similarity_groups_library_program_id ON public.similarity_groups USING btree (library_program_id);


--
-- Name: ix_tag_references_connection_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tag_references_connection_id ON public.tag_references USING btree (connection_id);


--
-- Name: ix_task_failure_evidence_asset; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_task_failure_evidence_asset ON public.task_failure_evidence USING btree (asset_id, created_at);


--
-- Name: ix_task_failure_evidence_error; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_task_failure_evidence_error ON public.task_failure_evidence USING btree (error_class, created_at);


--
-- Name: ix_task_failure_evidence_generation; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_task_failure_evidence_generation ON public.task_failure_evidence USING btree (analysis_generation_id);


--
-- Name: ix_task_failure_evidence_task; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_task_failure_evidence_task ON public.task_failure_evidence USING btree (task_id, created_at);


--
-- Name: ix_tasks_analysis_generation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tasks_analysis_generation_id ON public.tasks USING btree (analysis_generation_id);


--
-- Name: ix_tasks_asset_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tasks_asset_id ON public.tasks USING btree (asset_id);


--
-- Name: ix_tasks_asset_revision_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tasks_asset_revision_id ON public.tasks USING btree (asset_revision_id);


--
-- Name: ix_tasks_asset_revision_state; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tasks_asset_revision_state ON public.tasks USING btree (asset_id, asset_revision_id, state);


--
-- Name: ix_tasks_batch_state; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tasks_batch_state ON public.tasks USING btree (processing_batch_id, state);


--
-- Name: ix_tasks_campaign_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tasks_campaign_id ON public.tasks USING btree (campaign_id);


--
-- Name: ix_tasks_connection_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tasks_connection_id ON public.tasks USING btree (connection_id);


--
-- Name: ix_tasks_connection_state; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tasks_connection_state ON public.tasks USING btree (connection_id, state);


--
-- Name: ix_tasks_expired_lease; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tasks_expired_lease ON public.tasks USING btree (lease_expires_at) WHERE ((state)::text = ANY ((ARRAY['RUNNING'::character varying, 'CANCEL_REQUESTED'::character varying])::text[]));


--
-- Name: ix_tasks_leased_to_worker_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tasks_leased_to_worker_id ON public.tasks USING btree (leased_to_worker_id);


--
-- Name: ix_tasks_parent_task_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tasks_parent_task_id ON public.tasks USING btree (parent_task_id);


--
-- Name: ix_tasks_processing_batch_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tasks_processing_batch_id ON public.tasks USING btree (processing_batch_id);


--
-- Name: ix_tasks_runnable_route; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tasks_runnable_route ON public.tasks USING btree (routing_key, data_class, priority, available_at) WHERE ((state)::text = ANY ((ARRAY['QUEUED'::character varying, 'RETRY_WAITING'::character varying])::text[]));


--
-- Name: ix_visual_embedding_asset; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_visual_embedding_asset ON public.visual_embeddings USING btree (asset_id);


--
-- Name: ix_visual_embedding_hnsw; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_visual_embedding_hnsw ON public.visual_embeddings USING hnsw (embedding public.halfvec_cosine_ops);


--
-- Name: ix_visual_embedding_purpose_version; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_visual_embedding_purpose_version ON public.visual_embeddings USING btree (purpose, algorithm_version);


--
-- Name: ix_visual_embeddings_analysis_generation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_visual_embeddings_analysis_generation_id ON public.visual_embeddings USING btree (analysis_generation_id);


--
-- Name: ix_visual_fingerprint_band_lookup; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_visual_fingerprint_band_lookup ON public.visual_fingerprint_bands USING btree (algorithm_version, band_index, band_value);


--
-- Name: ix_visual_fingerprint_bands_asset_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_visual_fingerprint_bands_asset_id ON public.visual_fingerprint_bands USING btree (asset_id);


--
-- Name: ix_visual_fingerprint_bands_fingerprint_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_visual_fingerprint_bands_fingerprint_id ON public.visual_fingerprint_bands USING btree (fingerprint_id);


--
-- Name: ix_visual_fingerprints_asset_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_visual_fingerprints_asset_id ON public.visual_fingerprints USING btree (asset_id);


--
-- Name: ix_visual_fingerprints_similarity_checked_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_visual_fingerprints_similarity_checked_at ON public.visual_fingerprints USING btree (similarity_checked_at);


--
-- Name: ix_worker_credentials_worker_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_worker_credentials_worker_id ON public.worker_credentials USING btree (worker_id);


--
-- Name: ix_worker_model_availability_available; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_worker_model_availability_available ON public.worker_model_availability USING btree (available);


--
-- Name: ix_worker_model_availability_last_seen_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_worker_model_availability_last_seen_at ON public.worker_model_availability USING btree (last_seen_at);


--
-- Name: ix_worker_model_availability_model_version_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_worker_model_availability_model_version_id ON public.worker_model_availability USING btree (model_version_id);


--
-- Name: ix_worker_model_availability_worker_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_worker_model_availability_worker_id ON public.worker_model_availability USING btree (worker_id);


--
-- Name: ix_workers_affinity_processing_batch_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_workers_affinity_processing_batch_id ON public.workers USING btree (affinity_processing_batch_id);


--
-- Name: ix_workspace_presets_owner_kind; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_workspace_presets_owner_kind ON public.workspace_presets USING btree (owner_id, kind);


--
-- Name: ix_writeback_batches_materialization_state; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_writeback_batches_materialization_state ON public.writeback_batches USING btree (materialization_state);


--
-- Name: ix_writeback_operations_asset_revision_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_writeback_operations_asset_revision_id ON public.writeback_operations USING btree (asset_revision_id);


--
-- Name: ix_writeback_operations_batch_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_writeback_operations_batch_id ON public.writeback_operations USING btree (batch_id);


--
-- Name: ix_writeback_operations_batch_state_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_writeback_operations_batch_state_id ON public.writeback_operations USING btree (batch_id, state, id);


--
-- Name: uq_deep_review_prompt_experiment_running; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_deep_review_prompt_experiment_running ON public.deep_review_prompt_experiments USING btree (singleton_key) WHERE ((state)::text = 'running'::text);


--
-- Name: uq_owner_rating_sessions_one_active_per_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_owner_rating_sessions_one_active_per_owner ON public.owner_rating_sessions USING btree (owner_id) WHERE ((state)::text = 'active'::text);


--
-- Name: uq_review_reanalysis_active_program; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_review_reanalysis_active_program ON public.review_reanalysis_runs USING btree (program_id) WHERE ((state)::text = ANY ((ARRAY['running'::character varying, 'draining'::character varying, 'paused'::character varying])::text[]));


--
-- Name: uq_search_backfill_runs_active; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_search_backfill_runs_active ON public.search_backfill_runs USING btree (singleton_key) WHERE ((state)::text = ANY ((ARRAY['running'::character varying, 'draining'::character varying])::text[]));


--
-- Name: uq_tasks_active_primary_generation; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_tasks_active_primary_generation ON public.tasks USING btree (analysis_generation_id) WHERE ((analysis_generation_id IS NOT NULL) AND ((task_type)::text = 'asset.photo_dossier'::text) AND ((state)::text = ANY ((ARRAY['PLANNED'::character varying, 'QUEUED'::character varying, 'RUNNING'::character varying, 'RETRY_WAITING'::character varying, 'CANCEL_REQUESTED'::character varying])::text[])));


--
-- Name: ux_asset_curation_library_points; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ux_asset_curation_library_points ON public.asset_curation_snapshots USING btree (library_points);


--
-- Name: ux_asset_curation_library_position; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ux_asset_curation_library_position ON public.asset_curation_snapshots USING btree (library_position);


--
-- Name: album_asset_references trg_album_assets_live_domains; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_album_assets_live_domains AFTER INSERT OR DELETE OR UPDATE ON public.album_asset_references FOR EACH ROW EXECUTE FUNCTION public.star_emit_system_change('immich.changed', 'asset.changed');


--
-- Name: album_references trg_album_references_live_domains; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_album_references_live_domains AFTER INSERT OR DELETE OR UPDATE ON public.album_references FOR EACH ROW EXECUTE FUNCTION public.star_emit_system_change('immich.changed');


--
-- Name: analysis_runs trg_analysis_runs_system_change; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_analysis_runs_system_change AFTER INSERT OR DELETE OR UPDATE ON public.analysis_runs FOR EACH ROW EXECUTE FUNCTION public.star_emit_system_change('coordinator.changed', 'asset.changed', 'collection.changed');


--
-- Name: asset_curation_snapshots trg_asset_curation_live_domains; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_asset_curation_live_domains AFTER INSERT OR DELETE OR UPDATE ON public.asset_curation_snapshots FOR EACH ROW EXECUTE FUNCTION public.star_emit_system_change('asset.changed', 'collection.changed');


--
-- Name: asset_face_references trg_asset_faces_live_domains; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_asset_faces_live_domains AFTER INSERT OR DELETE OR UPDATE ON public.asset_face_references FOR EACH ROW EXECUTE FUNCTION public.star_emit_system_change('immich.changed', 'asset.changed');


--
-- Name: asset_immich_contexts trg_asset_immich_contexts_live_domains; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_asset_immich_contexts_live_domains AFTER INSERT OR DELETE OR UPDATE ON public.asset_immich_contexts FOR EACH ROW EXECUTE FUNCTION public.star_emit_system_change('immich.changed', 'asset.changed');


--
-- Name: asset_review_cycles trg_asset_review_cycles_system_change; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_asset_review_cycles_system_change AFTER INSERT OR DELETE OR UPDATE ON public.asset_review_cycles FOR EACH ROW EXECUTE FUNCTION public.star_emit_system_change('task.changed');


--
-- Name: asset_revisions trg_asset_revisions_live_domains; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_asset_revisions_live_domains AFTER INSERT OR DELETE OR UPDATE ON public.asset_revisions FOR EACH ROW EXECUTE FUNCTION public.star_emit_system_change('asset.changed');


--
-- Name: assets trg_assets_live_domains; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_assets_live_domains AFTER INSERT OR DELETE OR UPDATE ON public.assets FOR EACH ROW EXECUTE FUNCTION public.star_emit_system_change('asset.changed');


--
-- Name: audit_checkpoints trg_audit_checkpoints_live_domains; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_audit_checkpoints_live_domains AFTER INSERT OR DELETE OR UPDATE ON public.audit_checkpoints FOR EACH ROW EXECUTE FUNCTION public.star_emit_system_change('audit.changed');


--
-- Name: audit_events trg_audit_events_live_domains; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_audit_events_live_domains AFTER INSERT OR DELETE OR UPDATE ON public.audit_events FOR EACH ROW EXECUTE FUNCTION public.star_emit_system_change('audit.changed');


--
-- Name: collection_context_snapshots trg_collection_context_live_domains; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_collection_context_live_domains AFTER INSERT OR DELETE OR UPDATE ON public.collection_context_snapshots FOR EACH ROW EXECUTE FUNCTION public.star_emit_system_change('collection.changed');


--
-- Name: collection_groups trg_collection_groups_live_domains; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_collection_groups_live_domains AFTER INSERT OR DELETE OR UPDATE ON public.collection_groups FOR EACH ROW EXECUTE FUNCTION public.star_emit_system_change('collection.changed');


--
-- Name: collection_group_members trg_collection_members_live_domains; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_collection_members_live_domains AFTER INSERT OR DELETE OR UPDATE ON public.collection_group_members FOR EACH ROW EXECUTE FUNCTION public.star_emit_system_change('collection.changed');


--
-- Name: deep_review_backfills trg_deep_review_backfills_system_change; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_deep_review_backfills_system_change AFTER INSERT OR DELETE OR UPDATE ON public.deep_review_backfills FOR EACH ROW EXECUTE FUNCTION public.star_emit_system_change('program.changed');


--
-- Name: collection_groups trg_group_evidence_update; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_group_evidence_update AFTER UPDATE OF confidence, source_facts ON public.collection_groups FOR EACH ROW EXECUTE FUNCTION public.propagate_group_member_evidence();


--
-- Name: collection_group_members trg_group_member_evidence; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_group_member_evidence BEFORE INSERT OR UPDATE OF group_id ON public.collection_group_members FOR EACH ROW EXECUTE FUNCTION public.sync_group_member_evidence();


--
-- Name: immich_connections trg_immich_connections_live_domains; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_immich_connections_live_domains AFTER INSERT OR DELETE OR UPDATE ON public.immich_connections FOR EACH ROW EXECUTE FUNCTION public.star_emit_system_change('immich.changed');


--
-- Name: immich_instances trg_immich_instances_live_domains; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_immich_instances_live_domains AFTER INSERT OR DELETE OR UPDATE ON public.immich_instances FOR EACH ROW EXECUTE FUNCTION public.star_emit_system_change('immich.changed');


--
-- Name: library_programs trg_library_programs_system_change; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_library_programs_system_change AFTER INSERT OR DELETE OR UPDATE ON public.library_programs FOR EACH ROW EXECUTE FUNCTION public.star_emit_system_change('program.changed');


--
-- Name: model_deployments trg_model_deployments_live_domains; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_model_deployments_live_domains AFTER INSERT OR DELETE OR UPDATE ON public.model_deployments FOR EACH ROW EXECUTE FUNCTION public.star_emit_system_change('model.changed');


--
-- Name: model_packages trg_model_packages_live_domains; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_model_packages_live_domains AFTER INSERT OR DELETE OR UPDATE ON public.model_packages FOR EACH ROW EXECUTE FUNCTION public.star_emit_system_change('model.changed');


--
-- Name: model_versions trg_model_versions_live_domains; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_model_versions_live_domains AFTER INSERT OR DELETE OR UPDATE ON public.model_versions FOR EACH ROW EXECUTE FUNCTION public.star_emit_system_change('model.changed');


--
-- Name: processing_batches trg_processing_batches_system_change; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_processing_batches_system_change AFTER INSERT OR DELETE OR UPDATE ON public.processing_batches FOR EACH ROW EXECUTE FUNCTION public.star_emit_system_change('batch.changed');


--
-- Name: service_heartbeats trg_service_heartbeats_system_change; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_service_heartbeats_system_change AFTER INSERT OR DELETE OR UPDATE ON public.service_heartbeats FOR EACH ROW EXECUTE FUNCTION public.star_emit_system_change('coordinator.changed');


--
-- Name: storage_inventories trg_storage_inventories_live_domains; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_storage_inventories_live_domains AFTER INSERT OR DELETE OR UPDATE ON public.storage_inventories FOR EACH ROW EXECUTE FUNCTION public.star_emit_system_change('storage.changed');


--
-- Name: tag_references trg_tag_references_live_domains; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_tag_references_live_domains AFTER INSERT OR DELETE OR UPDATE ON public.tag_references FOR EACH ROW EXECUTE FUNCTION public.star_emit_system_change('immich.changed');


--
-- Name: tasks trg_tasks_system_change; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_tasks_system_change AFTER INSERT OR DELETE OR UPDATE ON public.tasks FOR EACH ROW EXECUTE FUNCTION public.star_emit_system_change('task.changed');


--
-- Name: workers trg_workers_system_change; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_workers_system_change AFTER INSERT OR DELETE OR UPDATE ON public.workers FOR EACH ROW EXECUTE FUNCTION public.star_emit_system_change('worker.changed');


--
-- Name: writeback_batches trg_writeback_batches_system_change; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_writeback_batches_system_change AFTER INSERT OR DELETE OR UPDATE ON public.writeback_batches FOR EACH ROW EXECUTE FUNCTION public.star_emit_system_change('writeback.changed');


--
-- Name: writeback_operations trg_writeback_operations_system_change; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_writeback_operations_system_change AFTER INSERT OR DELETE OR UPDATE ON public.writeback_operations FOR EACH ROW EXECUTE FUNCTION public.star_emit_system_change('writeback.changed');


--
-- Name: asset_ml_scores asset_ml_scores_asset_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.asset_ml_scores
    ADD CONSTRAINT asset_ml_scores_asset_id_fkey FOREIGN KEY (asset_id) REFERENCES public.assets(id) ON DELETE CASCADE;


--
-- Name: asset_ml_scores asset_ml_scores_personal_model_version_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.asset_ml_scores
    ADD CONSTRAINT asset_ml_scores_personal_model_version_id_fkey FOREIGN KEY (personal_model_version_id) REFERENCES public.personal_ranker_versions(id) ON DELETE SET NULL;


--
-- Name: asset_ml_scores asset_ml_scores_student_model_version_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.asset_ml_scores
    ADD CONSTRAINT asset_ml_scores_student_model_version_id_fkey FOREIGN KEY (student_model_version_id) REFERENCES public.ensemble_student_versions(id) ON DELETE SET NULL;


--
-- Name: asset_pipeline_state asset_pipeline_state_asset_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.asset_pipeline_state
    ADD CONSTRAINT asset_pipeline_state_asset_id_fkey FOREIGN KEY (asset_id) REFERENCES public.assets(id) ON DELETE CASCADE;


--
-- Name: asset_pipeline_state asset_pipeline_state_asset_revision_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.asset_pipeline_state
    ADD CONSTRAINT asset_pipeline_state_asset_revision_id_fkey FOREIGN KEY (asset_revision_id) REFERENCES public.asset_revisions(id) ON DELETE SET NULL;


--
-- Name: asset_ratings asset_ratings_asset_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.asset_ratings
    ADD CONSTRAINT asset_ratings_asset_id_fkey FOREIGN KEY (asset_id) REFERENCES public.assets(id) ON DELETE CASCADE;


--
-- Name: asset_search_projections asset_search_projections_asset_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.asset_search_projections
    ADD CONSTRAINT asset_search_projections_asset_id_fkey FOREIGN KEY (asset_id) REFERENCES public.assets(id) ON DELETE CASCADE;


--
-- Name: album_asset_references fk_album_asset_references_album_id_album_references; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.album_asset_references
    ADD CONSTRAINT fk_album_asset_references_album_id_album_references FOREIGN KEY (album_id) REFERENCES public.album_references(id) ON DELETE CASCADE;


--
-- Name: album_asset_references fk_album_asset_references_asset_id_assets; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.album_asset_references
    ADD CONSTRAINT fk_album_asset_references_asset_id_assets FOREIGN KEY (asset_id) REFERENCES public.assets(id) ON DELETE CASCADE;


--
-- Name: album_references fk_album_references_connection_id_immich_connections; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.album_references
    ADD CONSTRAINT fk_album_references_connection_id_immich_connections FOREIGN KEY (connection_id) REFERENCES public.immich_connections(id) ON DELETE CASCADE;


--
-- Name: analysis_generations fk_analysis_generations_asset_id_assets; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.analysis_generations
    ADD CONSTRAINT fk_analysis_generations_asset_id_assets FOREIGN KEY (asset_id) REFERENCES public.assets(id) ON DELETE CASCADE;


--
-- Name: analysis_generations fk_analysis_generations_asset_revision_id_asset_revisions; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.analysis_generations
    ADD CONSTRAINT fk_analysis_generations_asset_revision_id_asset_revisions FOREIGN KEY (asset_revision_id) REFERENCES public.asset_revisions(id) ON DELETE CASCADE;


--
-- Name: analysis_generations fk_analysis_generations_created_by_users; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.analysis_generations
    ADD CONSTRAINT fk_analysis_generations_created_by_users FOREIGN KEY (created_by) REFERENCES public.users(id) ON DELETE SET NULL;


--
-- Name: analysis_generations fk_analysis_generations_parent_generation_id_analysis_g_66c4; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.analysis_generations
    ADD CONSTRAINT fk_analysis_generations_parent_generation_id_analysis_g_66c4 FOREIGN KEY (parent_generation_id) REFERENCES public.analysis_generations(id) ON DELETE SET NULL;


--
-- Name: analysis_generations fk_analysis_generations_personal_ranker_version; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.analysis_generations
    ADD CONSTRAINT fk_analysis_generations_personal_ranker_version FOREIGN KEY (personal_ranker_version_id) REFERENCES public.personal_ranker_versions(id) ON DELETE SET NULL;


--
-- Name: analysis_nodes fk_analysis_nodes_asset_id_assets; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.analysis_nodes
    ADD CONSTRAINT fk_analysis_nodes_asset_id_assets FOREIGN KEY (asset_id) REFERENCES public.assets(id) ON DELETE CASCADE;


--
-- Name: analysis_nodes fk_analysis_nodes_generation_id_analysis_generations; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.analysis_nodes
    ADD CONSTRAINT fk_analysis_nodes_generation_id_analysis_generations FOREIGN KEY (generation_id) REFERENCES public.analysis_generations(id) ON DELETE CASCADE;


--
-- Name: analysis_nodes fk_analysis_nodes_result_analysis_run_id_analysis_runs; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.analysis_nodes
    ADD CONSTRAINT fk_analysis_nodes_result_analysis_run_id_analysis_runs FOREIGN KEY (result_analysis_run_id) REFERENCES public.analysis_runs(id) ON DELETE SET NULL;


--
-- Name: analysis_nodes fk_analysis_nodes_superseded_by_node_id_analysis_nodes; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.analysis_nodes
    ADD CONSTRAINT fk_analysis_nodes_superseded_by_node_id_analysis_nodes FOREIGN KEY (superseded_by_node_id) REFERENCES public.analysis_nodes(id) ON DELETE SET NULL;


--
-- Name: analysis_nodes fk_analysis_nodes_task_id_tasks; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.analysis_nodes
    ADD CONSTRAINT fk_analysis_nodes_task_id_tasks FOREIGN KEY (task_id) REFERENCES public.tasks(id) ON DELETE SET NULL;


--
-- Name: analysis_obligations fk_analysis_obligations_asset_id_assets; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.analysis_obligations
    ADD CONSTRAINT fk_analysis_obligations_asset_id_assets FOREIGN KEY (asset_id) REFERENCES public.assets(id) ON DELETE CASCADE;


--
-- Name: analysis_obligations fk_analysis_obligations_asset_revision_id_asset_revisions; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.analysis_obligations
    ADD CONSTRAINT fk_analysis_obligations_asset_revision_id_asset_revisions FOREIGN KEY (asset_revision_id) REFERENCES public.asset_revisions(id) ON DELETE CASCADE;


--
-- Name: analysis_obligations fk_analysis_obligations_library_program_id_library_programs; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.analysis_obligations
    ADD CONSTRAINT fk_analysis_obligations_library_program_id_library_programs FOREIGN KEY (library_program_id) REFERENCES public.library_programs(id) ON DELETE SET NULL;


--
-- Name: analysis_obligations fk_analysis_obligations_processing_batch_id_processing_batches; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.analysis_obligations
    ADD CONSTRAINT fk_analysis_obligations_processing_batch_id_processing_batches FOREIGN KEY (processing_batch_id) REFERENCES public.processing_batches(id) ON DELETE SET NULL;


--
-- Name: analysis_obligations fk_analysis_obligations_review_cycle_id_asset_review_cycles; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.analysis_obligations
    ADD CONSTRAINT fk_analysis_obligations_review_cycle_id_asset_review_cycles FOREIGN KEY (review_cycle_id) REFERENCES public.asset_review_cycles(id) ON DELETE SET NULL;


--
-- Name: analysis_obligations fk_analysis_obligations_task_id_tasks; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.analysis_obligations
    ADD CONSTRAINT fk_analysis_obligations_task_id_tasks FOREIGN KEY (task_id) REFERENCES public.tasks(id) ON DELETE SET NULL;


--
-- Name: analysis_runs fk_analysis_runs_asset_id_assets; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.analysis_runs
    ADD CONSTRAINT fk_analysis_runs_asset_id_assets FOREIGN KEY (asset_id) REFERENCES public.assets(id) ON DELETE CASCADE;


--
-- Name: analysis_runs fk_analysis_runs_asset_revision_id_asset_revisions; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.analysis_runs
    ADD CONSTRAINT fk_analysis_runs_asset_revision_id_asset_revisions FOREIGN KEY (asset_revision_id) REFERENCES public.asset_revisions(id) ON DELETE CASCADE;


--
-- Name: analysis_runs fk_analysis_runs_campaign_id_cleanup_campaigns; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.analysis_runs
    ADD CONSTRAINT fk_analysis_runs_campaign_id_cleanup_campaigns FOREIGN KEY (campaign_id) REFERENCES public.cleanup_campaigns(id) ON DELETE SET NULL;


--
-- Name: analysis_runs fk_analysis_runs_generation; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.analysis_runs
    ADD CONSTRAINT fk_analysis_runs_generation FOREIGN KEY (analysis_generation_id) REFERENCES public.analysis_generations(id) ON DELETE SET NULL;


--
-- Name: analysis_runs fk_analysis_runs_library_program_id_library_programs; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.analysis_runs
    ADD CONSTRAINT fk_analysis_runs_library_program_id_library_programs FOREIGN KEY (library_program_id) REFERENCES public.library_programs(id) ON DELETE SET NULL;


--
-- Name: analysis_runs fk_analysis_runs_model_version_id_model_versions; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.analysis_runs
    ADD CONSTRAINT fk_analysis_runs_model_version_id_model_versions FOREIGN KEY (model_version_id) REFERENCES public.model_versions(id) ON DELETE SET NULL;


--
-- Name: api_credentials fk_api_credentials_connection_id_immich_connections; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.api_credentials
    ADD CONSTRAINT fk_api_credentials_connection_id_immich_connections FOREIGN KEY (connection_id) REFERENCES public.immich_connections(id) ON DELETE CASCADE;


--
-- Name: apple_vision_evidence fk_apple_vision_evidence_analysis_run_id_analysis_runs; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.apple_vision_evidence
    ADD CONSTRAINT fk_apple_vision_evidence_analysis_run_id_analysis_runs FOREIGN KEY (analysis_run_id) REFERENCES public.analysis_runs(id) ON DELETE CASCADE;


--
-- Name: apple_vision_evidence fk_apple_vision_evidence_asset_id_assets; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.apple_vision_evidence
    ADD CONSTRAINT fk_apple_vision_evidence_asset_id_assets FOREIGN KEY (asset_id) REFERENCES public.assets(id) ON DELETE CASCADE;


--
-- Name: apple_vision_evidence fk_apple_vision_evidence_asset_revision_id_asset_revisions; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.apple_vision_evidence
    ADD CONSTRAINT fk_apple_vision_evidence_asset_revision_id_asset_revisions FOREIGN KEY (asset_revision_id) REFERENCES public.asset_revisions(id) ON DELETE CASCADE;


--
-- Name: asset_curation_snapshots fk_asset_curation_snapshots_asset_id_assets; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.asset_curation_snapshots
    ADD CONSTRAINT fk_asset_curation_snapshots_asset_id_assets FOREIGN KEY (asset_id) REFERENCES public.assets(id) ON DELETE CASCADE;


--
-- Name: asset_curation_snapshots fk_asset_curation_snapshots_asset_revision_id_asset_revisions; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.asset_curation_snapshots
    ADD CONSTRAINT fk_asset_curation_snapshots_asset_revision_id_asset_revisions FOREIGN KEY (asset_revision_id) REFERENCES public.asset_revisions(id) ON DELETE SET NULL;


--
-- Name: asset_curation_snapshots fk_asset_curation_snapshots_personal_model_version_id_p_24f4; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.asset_curation_snapshots
    ADD CONSTRAINT fk_asset_curation_snapshots_personal_model_version_id_p_24f4 FOREIGN KEY (personal_model_version_id) REFERENCES public.personal_ranker_versions(id) ON DELETE SET NULL;


--
-- Name: asset_curation_snapshots fk_asset_curation_student_model; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.asset_curation_snapshots
    ADD CONSTRAINT fk_asset_curation_student_model FOREIGN KEY (student_model_version_id) REFERENCES public.ensemble_student_versions(id) ON DELETE SET NULL;


--
-- Name: asset_face_references fk_asset_face_references_asset_id_assets; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.asset_face_references
    ADD CONSTRAINT fk_asset_face_references_asset_id_assets FOREIGN KEY (asset_id) REFERENCES public.assets(id) ON DELETE CASCADE;


--
-- Name: asset_face_references fk_asset_face_references_connection_id_immich_connections; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.asset_face_references
    ADD CONSTRAINT fk_asset_face_references_connection_id_immich_connections FOREIGN KEY (connection_id) REFERENCES public.immich_connections(id) ON DELETE CASCADE;


--
-- Name: asset_immich_contexts fk_asset_immich_contexts_asset_id_assets; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.asset_immich_contexts
    ADD CONSTRAINT fk_asset_immich_contexts_asset_id_assets FOREIGN KEY (asset_id) REFERENCES public.assets(id) ON DELETE CASCADE;


--
-- Name: asset_metrics fk_asset_metrics_analysis_run_id_analysis_runs; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.asset_metrics
    ADD CONSTRAINT fk_asset_metrics_analysis_run_id_analysis_runs FOREIGN KEY (analysis_run_id) REFERENCES public.analysis_runs(id) ON DELETE CASCADE;


--
-- Name: asset_review_cycles fk_asset_review_cycles_asset_id_assets; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.asset_review_cycles
    ADD CONSTRAINT fk_asset_review_cycles_asset_id_assets FOREIGN KEY (asset_id) REFERENCES public.assets(id) ON DELETE CASCADE;


--
-- Name: asset_review_cycles fk_asset_review_cycles_asset_revision_id_asset_revisions; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.asset_review_cycles
    ADD CONSTRAINT fk_asset_review_cycles_asset_revision_id_asset_revisions FOREIGN KEY (asset_revision_id) REFERENCES public.asset_revisions(id) ON DELETE CASCADE;


--
-- Name: asset_review_cycles fk_asset_review_cycles_library_program_id_library_programs; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.asset_review_cycles
    ADD CONSTRAINT fk_asset_review_cycles_library_program_id_library_programs FOREIGN KEY (library_program_id) REFERENCES public.library_programs(id) ON DELETE SET NULL;


--
-- Name: asset_review_cycles fk_asset_review_cycles_processing_batch_id_processing_batches; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.asset_review_cycles
    ADD CONSTRAINT fk_asset_review_cycles_processing_batch_id_processing_batches FOREIGN KEY (processing_batch_id) REFERENCES public.processing_batches(id) ON DELETE SET NULL;


--
-- Name: asset_revisions fk_asset_revisions_asset_id_assets; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.asset_revisions
    ADD CONSTRAINT fk_asset_revisions_asset_id_assets FOREIGN KEY (asset_id) REFERENCES public.assets(id) ON DELETE CASCADE;


--
-- Name: asset_search_embeddings fk_asset_search_embeddings_analysis_generation_id_analy_1d7c; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.asset_search_embeddings
    ADD CONSTRAINT fk_asset_search_embeddings_analysis_generation_id_analy_1d7c FOREIGN KEY (analysis_generation_id) REFERENCES public.analysis_generations(id) ON DELETE SET NULL;


--
-- Name: asset_search_embeddings fk_asset_search_embeddings_analysis_run_id_analysis_runs; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.asset_search_embeddings
    ADD CONSTRAINT fk_asset_search_embeddings_analysis_run_id_analysis_runs FOREIGN KEY (analysis_run_id) REFERENCES public.analysis_runs(id) ON DELETE CASCADE;


--
-- Name: asset_search_embeddings fk_asset_search_embeddings_asset_id_assets; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.asset_search_embeddings
    ADD CONSTRAINT fk_asset_search_embeddings_asset_id_assets FOREIGN KEY (asset_id) REFERENCES public.assets(id) ON DELETE CASCADE;


--
-- Name: asset_search_embeddings fk_asset_search_embeddings_asset_revision_id_asset_revisions; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.asset_search_embeddings
    ADD CONSTRAINT fk_asset_search_embeddings_asset_revision_id_asset_revisions FOREIGN KEY (asset_revision_id) REFERENCES public.asset_revisions(id) ON DELETE CASCADE;


--
-- Name: assets fk_assets_connection_id_immich_connections; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assets
    ADD CONSTRAINT fk_assets_connection_id_immich_connections FOREIGN KEY (connection_id) REFERENCES public.immich_connections(id) ON DELETE CASCADE;


--
-- Name: assets fk_assets_current_generation_id_analysis_generations; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assets
    ADD CONSTRAINT fk_assets_current_generation_id_analysis_generations FOREIGN KEY (current_generation_id) REFERENCES public.analysis_generations(id) ON DELETE SET NULL;


--
-- Name: assets fk_assets_instance_id_immich_instances; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assets
    ADD CONSTRAINT fk_assets_instance_id_immich_instances FOREIGN KEY (instance_id) REFERENCES public.immich_instances(id) ON DELETE CASCADE;


--
-- Name: audit_access_tokens fk_audit_access_tokens_owner_id_users; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audit_access_tokens
    ADD CONSTRAINT fk_audit_access_tokens_owner_id_users FOREIGN KEY (owner_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: audit_checkpoints fk_audit_checkpoints_event_id_audit_events; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audit_checkpoints
    ADD CONSTRAINT fk_audit_checkpoints_event_id_audit_events FOREIGN KEY (event_id) REFERENCES public.audit_events(id) ON DELETE RESTRICT;


--
-- Name: campaign_policy_versions fk_campaign_policy_versions_campaign_id_cleanup_campaigns; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.campaign_policy_versions
    ADD CONSTRAINT fk_campaign_policy_versions_campaign_id_cleanup_campaigns FOREIGN KEY (campaign_id) REFERENCES public.cleanup_campaigns(id) ON DELETE CASCADE;


--
-- Name: cleanup_campaigns fk_cleanup_campaigns_created_by_users; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cleanup_campaigns
    ADD CONSTRAINT fk_cleanup_campaigns_created_by_users FOREIGN KEY (created_by) REFERENCES public.users(id);


--
-- Name: cleanup_campaigns fk_cleanup_campaigns_library_program_id_library_programs; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cleanup_campaigns
    ADD CONSTRAINT fk_cleanup_campaigns_library_program_id_library_programs FOREIGN KEY (library_program_id) REFERENCES public.library_programs(id) ON DELETE SET NULL;


--
-- Name: collection_context_snapshots fk_collection_context_snapshots_asset_id_assets; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.collection_context_snapshots
    ADD CONSTRAINT fk_collection_context_snapshots_asset_id_assets FOREIGN KEY (asset_id) REFERENCES public.assets(id) ON DELETE CASCADE;


--
-- Name: collection_context_snapshots fk_collection_context_snapshots_asset_revision_id_asset_9a93; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.collection_context_snapshots
    ADD CONSTRAINT fk_collection_context_snapshots_asset_revision_id_asset_9a93 FOREIGN KEY (asset_revision_id) REFERENCES public.asset_revisions(id) ON DELETE CASCADE;


--
-- Name: collection_group_members fk_collection_group_members_asset_id_assets; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.collection_group_members
    ADD CONSTRAINT fk_collection_group_members_asset_id_assets FOREIGN KEY (asset_id) REFERENCES public.assets(id) ON DELETE CASCADE;


--
-- Name: collection_group_members fk_collection_group_members_asset_revision_id_asset_revisions; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.collection_group_members
    ADD CONSTRAINT fk_collection_group_members_asset_revision_id_asset_revisions FOREIGN KEY (asset_revision_id) REFERENCES public.asset_revisions(id) ON DELETE CASCADE;


--
-- Name: collection_group_members fk_collection_group_members_group_id_collection_groups; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.collection_group_members
    ADD CONSTRAINT fk_collection_group_members_group_id_collection_groups FOREIGN KEY (group_id) REFERENCES public.collection_groups(id) ON DELETE CASCADE;


--
-- Name: collection_groups fk_collection_groups_connection_id_immich_connections; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.collection_groups
    ADD CONSTRAINT fk_collection_groups_connection_id_immich_connections FOREIGN KEY (connection_id) REFERENCES public.immich_connections(id) ON DELETE CASCADE;


--
-- Name: collection_groups fk_collection_groups_manual_representative_asset_id_assets; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.collection_groups
    ADD CONSTRAINT fk_collection_groups_manual_representative_asset_id_assets FOREIGN KEY (manual_representative_asset_id) REFERENCES public.assets(id) ON DELETE SET NULL;


--
-- Name: collection_groups fk_collection_groups_representative_asset_id_assets; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.collection_groups
    ADD CONSTRAINT fk_collection_groups_representative_asset_id_assets FOREIGN KEY (representative_asset_id) REFERENCES public.assets(id) ON DELETE SET NULL;


--
-- Name: deep_review_backfills fk_deep_review_backfills_created_by_users; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.deep_review_backfills
    ADD CONSTRAINT fk_deep_review_backfills_created_by_users FOREIGN KEY (created_by) REFERENCES public.users(id) ON DELETE RESTRICT;


--
-- Name: deep_review_backfills fk_deep_review_backfills_program_id_library_programs; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.deep_review_backfills
    ADD CONSTRAINT fk_deep_review_backfills_program_id_library_programs FOREIGN KEY (program_id) REFERENCES public.library_programs(id) ON DELETE CASCADE;


--
-- Name: deep_review_prompt_comparisons fk_deep_review_prompt_comparisons_actor_id_users; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.deep_review_prompt_comparisons
    ADD CONSTRAINT fk_deep_review_prompt_comparisons_actor_id_users FOREIGN KEY (actor_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: deep_review_prompt_comparisons fk_deep_review_prompt_comparisons_asset_id_assets; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.deep_review_prompt_comparisons
    ADD CONSTRAINT fk_deep_review_prompt_comparisons_asset_id_assets FOREIGN KEY (asset_id) REFERENCES public.assets(id) ON DELETE CASCADE;


--
-- Name: deep_review_prompt_comparisons fk_deep_review_prompt_comparisons_experiment_id_deep_re_f287; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.deep_review_prompt_comparisons
    ADD CONSTRAINT fk_deep_review_prompt_comparisons_experiment_id_deep_re_f287 FOREIGN KEY (experiment_id) REFERENCES public.deep_review_prompt_experiments(id) ON DELETE CASCADE;


--
-- Name: deep_review_prompt_experiments fk_deep_review_prompt_experiments_created_by_users; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.deep_review_prompt_experiments
    ADD CONSTRAINT fk_deep_review_prompt_experiments_created_by_users FOREIGN KEY (created_by) REFERENCES public.users(id) ON DELETE RESTRICT;


--
-- Name: deep_review_prompt_experiments fk_deep_review_prompt_experiments_model_version_id_mode_9417; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.deep_review_prompt_experiments
    ADD CONSTRAINT fk_deep_review_prompt_experiments_model_version_id_mode_9417 FOREIGN KEY (model_version_id) REFERENCES public.model_versions(id) ON DELETE RESTRICT;


--
-- Name: deep_review_prompt_results fk_deep_review_prompt_results_asset_id_assets; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.deep_review_prompt_results
    ADD CONSTRAINT fk_deep_review_prompt_results_asset_id_assets FOREIGN KEY (asset_id) REFERENCES public.assets(id) ON DELETE CASCADE;


--
-- Name: deep_review_prompt_results fk_deep_review_prompt_results_asset_revision_id_asset_revisions; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.deep_review_prompt_results
    ADD CONSTRAINT fk_deep_review_prompt_results_asset_revision_id_asset_revisions FOREIGN KEY (asset_revision_id) REFERENCES public.asset_revisions(id) ON DELETE CASCADE;


--
-- Name: deep_review_prompt_results fk_deep_review_prompt_results_experiment_id_deep_review_c269; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.deep_review_prompt_results
    ADD CONSTRAINT fk_deep_review_prompt_results_experiment_id_deep_review_c269 FOREIGN KEY (experiment_id) REFERENCES public.deep_review_prompt_experiments(id) ON DELETE CASCADE;


--
-- Name: deep_review_prompt_results fk_deep_review_prompt_results_task_id_tasks; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.deep_review_prompt_results
    ADD CONSTRAINT fk_deep_review_prompt_results_task_id_tasks FOREIGN KEY (task_id) REFERENCES public.tasks(id) ON DELETE SET NULL;


--
-- Name: ensemble_student_training_runs fk_ensemble_student_training_runs_model_version_id_ense_8b12; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ensemble_student_training_runs
    ADD CONSTRAINT fk_ensemble_student_training_runs_model_version_id_ense_8b12 FOREIGN KEY (model_version_id) REFERENCES public.ensemble_student_versions(id) ON DELETE SET NULL;


--
-- Name: ensemble_student_training_runs fk_ensemble_student_training_runs_requested_by_users; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ensemble_student_training_runs
    ADD CONSTRAINT fk_ensemble_student_training_runs_requested_by_users FOREIGN KEY (requested_by) REFERENCES public.users(id) ON DELETE SET NULL;


--
-- Name: highlight_league_entries fk_highlight_entries_generation; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.highlight_league_entries
    ADD CONSTRAINT fk_highlight_entries_generation FOREIGN KEY (analysis_generation_id) REFERENCES public.analysis_generations(id) ON DELETE CASCADE;


--
-- Name: human_decisions fk_human_decisions_actor_id_users; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.human_decisions
    ADD CONSTRAINT fk_human_decisions_actor_id_users FOREIGN KEY (actor_id) REFERENCES public.users(id);


--
-- Name: human_decisions fk_human_decisions_campaign_id_cleanup_campaigns; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.human_decisions
    ADD CONSTRAINT fk_human_decisions_campaign_id_cleanup_campaigns FOREIGN KEY (campaign_id) REFERENCES public.cleanup_campaigns(id) ON DELETE SET NULL;


--
-- Name: human_decisions fk_human_decisions_supersedes_id_human_decisions; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.human_decisions
    ADD CONSTRAINT fk_human_decisions_supersedes_id_human_decisions FOREIGN KEY (supersedes_id) REFERENCES public.human_decisions(id);


--
-- Name: immich_connections fk_immich_connections_instance_id_immich_instances; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.immich_connections
    ADD CONSTRAINT fk_immich_connections_instance_id_immich_instances FOREIGN KEY (instance_id) REFERENCES public.immich_instances(id) ON DELETE CASCADE;


--
-- Name: inference_runs fk_inference_runs_analysis_generation_id_analysis_generations; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.inference_runs
    ADD CONSTRAINT fk_inference_runs_analysis_generation_id_analysis_generations FOREIGN KEY (analysis_generation_id) REFERENCES public.analysis_generations(id) ON DELETE SET NULL;


--
-- Name: inference_runs fk_inference_runs_analysis_run_id_analysis_runs; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.inference_runs
    ADD CONSTRAINT fk_inference_runs_analysis_run_id_analysis_runs FOREIGN KEY (analysis_run_id) REFERENCES public.analysis_runs(id) ON DELETE SET NULL;


--
-- Name: inference_runs fk_inference_runs_asset_id_assets; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.inference_runs
    ADD CONSTRAINT fk_inference_runs_asset_id_assets FOREIGN KEY (asset_id) REFERENCES public.assets(id) ON DELETE SET NULL;


--
-- Name: inference_runs fk_inference_runs_model_version_id_model_versions; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.inference_runs
    ADD CONSTRAINT fk_inference_runs_model_version_id_model_versions FOREIGN KEY (model_version_id) REFERENCES public.model_versions(id) ON DELETE SET NULL;


--
-- Name: inference_runs fk_inference_runs_task_id_tasks; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.inference_runs
    ADD CONSTRAINT fk_inference_runs_task_id_tasks FOREIGN KEY (task_id) REFERENCES public.tasks(id) ON DELETE SET NULL;


--
-- Name: library_policy_versions fk_library_policy_versions_created_by_users; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.library_policy_versions
    ADD CONSTRAINT fk_library_policy_versions_created_by_users FOREIGN KEY (created_by) REFERENCES public.users(id) ON DELETE SET NULL;


--
-- Name: library_policy_versions fk_library_policy_versions_program_id_library_programs; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.library_policy_versions
    ADD CONSTRAINT fk_library_policy_versions_program_id_library_programs FOREIGN KEY (program_id) REFERENCES public.library_programs(id) ON DELETE CASCADE;


--
-- Name: library_run_schedules fk_library_run_schedules_program_id_library_programs; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.library_run_schedules
    ADD CONSTRAINT fk_library_run_schedules_program_id_library_programs FOREIGN KEY (program_id) REFERENCES public.library_programs(id) ON DELETE CASCADE;


--
-- Name: model_deployments fk_model_deployments_model_package_id_model_packages; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.model_deployments
    ADD CONSTRAINT fk_model_deployments_model_package_id_model_packages FOREIGN KEY (model_package_id) REFERENCES public.model_packages(id) ON DELETE CASCADE;


--
-- Name: model_deployments fk_model_deployments_worker_id_workers; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.model_deployments
    ADD CONSTRAINT fk_model_deployments_worker_id_workers FOREIGN KEY (worker_id) REFERENCES public.workers(id) ON DELETE CASCADE;


--
-- Name: model_packages fk_model_packages_approved_by_users; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.model_packages
    ADD CONSTRAINT fk_model_packages_approved_by_users FOREIGN KEY (approved_by) REFERENCES public.users(id);


--
-- Name: model_packages fk_model_packages_model_version_id_model_versions; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.model_packages
    ADD CONSTRAINT fk_model_packages_model_version_id_model_versions FOREIGN KEY (model_version_id) REFERENCES public.model_versions(id) ON DELETE CASCADE;


--
-- Name: normalized_derivatives fk_normalized_derivatives_asset_id_assets; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.normalized_derivatives
    ADD CONSTRAINT fk_normalized_derivatives_asset_id_assets FOREIGN KEY (asset_id) REFERENCES public.assets(id) ON DELETE CASCADE;


--
-- Name: normalized_derivatives fk_normalized_derivatives_asset_revision_id_asset_revisions; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.normalized_derivatives
    ADD CONSTRAINT fk_normalized_derivatives_asset_revision_id_asset_revisions FOREIGN KEY (asset_revision_id) REFERENCES public.asset_revisions(id) ON DELETE CASCADE;


--
-- Name: operational_events fk_operational_events_connection_id_immich_connections; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.operational_events
    ADD CONSTRAINT fk_operational_events_connection_id_immich_connections FOREIGN KEY (connection_id) REFERENCES public.immich_connections(id) ON DELETE SET NULL;


--
-- Name: operational_events fk_operational_events_model_id_model_versions; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.operational_events
    ADD CONSTRAINT fk_operational_events_model_id_model_versions FOREIGN KEY (model_id) REFERENCES public.model_versions(id) ON DELETE SET NULL;


--
-- Name: operational_events fk_operational_events_task_id_tasks; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.operational_events
    ADD CONSTRAINT fk_operational_events_task_id_tasks FOREIGN KEY (task_id) REFERENCES public.tasks(id) ON DELETE SET NULL;


--
-- Name: operational_events fk_operational_events_worker_id_workers; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.operational_events
    ADD CONSTRAINT fk_operational_events_worker_id_workers FOREIGN KEY (worker_id) REFERENCES public.workers(id) ON DELETE SET NULL;


--
-- Name: owner_rating_session_items fk_owner_rating_session_items_asset_id_assets; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.owner_rating_session_items
    ADD CONSTRAINT fk_owner_rating_session_items_asset_id_assets FOREIGN KEY (asset_id) REFERENCES public.assets(id) ON DELETE CASCADE;


--
-- Name: owner_rating_session_items fk_owner_rating_session_items_asset_revision_id_asset_revisions; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.owner_rating_session_items
    ADD CONSTRAINT fk_owner_rating_session_items_asset_revision_id_asset_revisions FOREIGN KEY (asset_revision_id) REFERENCES public.asset_revisions(id) ON DELETE CASCADE;


--
-- Name: owner_rating_session_items fk_owner_rating_session_items_expected_rating_decision__5087; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.owner_rating_session_items
    ADD CONSTRAINT fk_owner_rating_session_items_expected_rating_decision__5087 FOREIGN KEY (expected_rating_decision_id) REFERENCES public.human_decisions(id) ON DELETE SET NULL;


--
-- Name: owner_rating_session_items fk_owner_rating_session_items_expected_sensitive_decisi_0191; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.owner_rating_session_items
    ADD CONSTRAINT fk_owner_rating_session_items_expected_sensitive_decisi_0191 FOREIGN KEY (expected_sensitive_decision_id) REFERENCES public.human_decisions(id) ON DELETE SET NULL;


--
-- Name: owner_rating_session_items fk_owner_rating_session_items_rating_decision_id_human__697a; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.owner_rating_session_items
    ADD CONSTRAINT fk_owner_rating_session_items_rating_decision_id_human__697a FOREIGN KEY (rating_decision_id) REFERENCES public.human_decisions(id) ON DELETE SET NULL;


--
-- Name: owner_rating_session_items fk_owner_rating_session_items_sensitive_decision_id_hum_b688; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.owner_rating_session_items
    ADD CONSTRAINT fk_owner_rating_session_items_sensitive_decision_id_hum_b688 FOREIGN KEY (sensitive_decision_id) REFERENCES public.human_decisions(id) ON DELETE SET NULL;


--
-- Name: owner_rating_session_items fk_owner_rating_session_items_session_id_owner_rating_sessions; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.owner_rating_session_items
    ADD CONSTRAINT fk_owner_rating_session_items_session_id_owner_rating_sessions FOREIGN KEY (session_id) REFERENCES public.owner_rating_sessions(id) ON DELETE CASCADE;


--
-- Name: owner_rating_sessions fk_owner_rating_sessions_owner_id_users; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.owner_rating_sessions
    ADD CONSTRAINT fk_owner_rating_sessions_owner_id_users FOREIGN KEY (owner_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: passkey_credentials fk_passkey_credentials_user_id_users; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.passkey_credentials
    ADD CONSTRAINT fk_passkey_credentials_user_id_users FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: person_references fk_person_references_connection_id_immich_connections; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.person_references
    ADD CONSTRAINT fk_person_references_connection_id_immich_connections FOREIGN KEY (connection_id) REFERENCES public.immich_connections(id) ON DELETE CASCADE;


--
-- Name: personal_ranker_versions fk_personal_ranker_versions_created_by_users; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.personal_ranker_versions
    ADD CONSTRAINT fk_personal_ranker_versions_created_by_users FOREIGN KEY (created_by) REFERENCES public.users(id);


--
-- Name: preference_lora_adapters fk_preference_lora_adapters_approved_by_users; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.preference_lora_adapters
    ADD CONSTRAINT fk_preference_lora_adapters_approved_by_users FOREIGN KEY (approved_by) REFERENCES public.users(id) ON DELETE SET NULL;


--
-- Name: preference_lora_adapters fk_preference_lora_adapters_run_id_preference_lora_runs; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.preference_lora_adapters
    ADD CONSTRAINT fk_preference_lora_adapters_run_id_preference_lora_runs FOREIGN KEY (run_id) REFERENCES public.preference_lora_runs(id) ON DELETE RESTRICT;


--
-- Name: preference_lora_runs fk_preference_lora_runs_created_by_users; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.preference_lora_runs
    ADD CONSTRAINT fk_preference_lora_runs_created_by_users FOREIGN KEY (created_by) REFERENCES public.users(id) ON DELETE RESTRICT;


--
-- Name: preference_lora_runs fk_preference_lora_runs_task_id_tasks; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.preference_lora_runs
    ADD CONSTRAINT fk_preference_lora_runs_task_id_tasks FOREIGN KEY (task_id) REFERENCES public.tasks(id) ON DELETE SET NULL;


--
-- Name: preference_photo_insights fk_preference_photo_insights_analysis_run_id_analysis_runs; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.preference_photo_insights
    ADD CONSTRAINT fk_preference_photo_insights_analysis_run_id_analysis_runs FOREIGN KEY (analysis_run_id) REFERENCES public.analysis_runs(id) ON DELETE SET NULL;


--
-- Name: preference_photo_insights fk_preference_photo_insights_asset_id_assets; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.preference_photo_insights
    ADD CONSTRAINT fk_preference_photo_insights_asset_id_assets FOREIGN KEY (asset_id) REFERENCES public.assets(id) ON DELETE CASCADE;


--
-- Name: preference_photo_insights fk_preference_photo_insights_asset_revision_id_asset_revisions; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.preference_photo_insights
    ADD CONSTRAINT fk_preference_photo_insights_asset_revision_id_asset_revisions FOREIGN KEY (asset_revision_id) REFERENCES public.asset_revisions(id) ON DELETE CASCADE;


--
-- Name: preference_photo_insights fk_preference_photo_insights_owner_decision_id_human_decisions; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.preference_photo_insights
    ADD CONSTRAINT fk_preference_photo_insights_owner_decision_id_human_decisions FOREIGN KEY (owner_decision_id) REFERENCES public.human_decisions(id) ON DELETE CASCADE;


--
-- Name: preference_photo_insights fk_preference_photo_insights_run_id_preference_prompt_runs; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.preference_photo_insights
    ADD CONSTRAINT fk_preference_photo_insights_run_id_preference_prompt_runs FOREIGN KEY (run_id) REFERENCES public.preference_prompt_runs(id) ON DELETE CASCADE;


--
-- Name: preference_photo_insights fk_preference_photo_insights_task_id_tasks; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.preference_photo_insights
    ADD CONSTRAINT fk_preference_photo_insights_task_id_tasks FOREIGN KEY (task_id) REFERENCES public.tasks(id) ON DELETE SET NULL;


--
-- Name: preference_prompt_runs fk_preference_prompt_runs_created_by_users; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.preference_prompt_runs
    ADD CONSTRAINT fk_preference_prompt_runs_created_by_users FOREIGN KEY (created_by) REFERENCES public.users(id) ON DELETE RESTRICT;


--
-- Name: preference_prompt_runs fk_preference_prompt_runs_personal_ranker_version_id_pe_64e9; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.preference_prompt_runs
    ADD CONSTRAINT fk_preference_prompt_runs_personal_ranker_version_id_pe_64e9 FOREIGN KEY (personal_ranker_version_id) REFERENCES public.personal_ranker_versions(id) ON DELETE SET NULL;


--
-- Name: preference_prompt_runs fk_preference_prompt_runs_proposed_revision_id_review_p_32d2; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.preference_prompt_runs
    ADD CONSTRAINT fk_preference_prompt_runs_proposed_revision_id_review_p_32d2 FOREIGN KEY (proposed_revision_id) REFERENCES public.review_prompt_revisions(id) ON DELETE SET NULL;


--
-- Name: preference_prompt_runs fk_preference_prompt_runs_synthesis_task_id_tasks; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.preference_prompt_runs
    ADD CONSTRAINT fk_preference_prompt_runs_synthesis_task_id_tasks FOREIGN KEY (synthesis_task_id) REFERENCES public.tasks(id) ON DELETE SET NULL;


--
-- Name: processing_batches fk_processing_batches_campaign_id_cleanup_campaigns; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.processing_batches
    ADD CONSTRAINT fk_processing_batches_campaign_id_cleanup_campaigns FOREIGN KEY (campaign_id) REFERENCES public.cleanup_campaigns(id) ON DELETE SET NULL;


--
-- Name: processing_batches fk_processing_batches_program_id_library_programs; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.processing_batches
    ADD CONSTRAINT fk_processing_batches_program_id_library_programs FOREIGN KEY (program_id) REFERENCES public.library_programs(id) ON DELETE CASCADE;


--
-- Name: proposals fk_proposals_campaign_id_cleanup_campaigns; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.proposals
    ADD CONSTRAINT fk_proposals_campaign_id_cleanup_campaigns FOREIGN KEY (campaign_id) REFERENCES public.cleanup_campaigns(id) ON DELETE CASCADE;


--
-- Name: proposals fk_proposals_generation; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.proposals
    ADD CONSTRAINT fk_proposals_generation FOREIGN KEY (analysis_generation_id) REFERENCES public.analysis_generations(id) ON DELETE SET NULL;


--
-- Name: analysis_publication_snapshots fk_pub_snap_asset; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.analysis_publication_snapshots
    ADD CONSTRAINT fk_pub_snap_asset FOREIGN KEY (asset_id) REFERENCES public.assets(id) ON DELETE CASCADE;


--
-- Name: analysis_publication_snapshots fk_pub_snap_generation; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.analysis_publication_snapshots
    ADD CONSTRAINT fk_pub_snap_generation FOREIGN KEY (analysis_generation_id) REFERENCES public.analysis_generations(id) ON DELETE CASCADE;


--
-- Name: analysis_publication_snapshots fk_pub_snap_revision; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.analysis_publication_snapshots
    ADD CONSTRAINT fk_pub_snap_revision FOREIGN KEY (asset_revision_id) REFERENCES public.asset_revisions(id) ON DELETE CASCADE;


--
-- Name: asset_review_cycles fk_review_cycles_generation; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.asset_review_cycles
    ADD CONSTRAINT fk_review_cycles_generation FOREIGN KEY (analysis_generation_id) REFERENCES public.analysis_generations(id) ON DELETE SET NULL;


--
-- Name: review_prompt_overrides fk_review_prompt_overrides_updated_by_users; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.review_prompt_overrides
    ADD CONSTRAINT fk_review_prompt_overrides_updated_by_users FOREIGN KEY (updated_by) REFERENCES public.users(id) ON DELETE SET NULL;


--
-- Name: review_prompt_revisions fk_review_prompt_revisions_activated_by_users; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.review_prompt_revisions
    ADD CONSTRAINT fk_review_prompt_revisions_activated_by_users FOREIGN KEY (activated_by) REFERENCES public.users(id) ON DELETE SET NULL;


--
-- Name: review_prompt_revisions fk_review_prompt_revisions_created_by_users; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.review_prompt_revisions
    ADD CONSTRAINT fk_review_prompt_revisions_created_by_users FOREIGN KEY (created_by) REFERENCES public.users(id) ON DELETE SET NULL;


--
-- Name: review_prompt_revisions fk_review_prompt_revisions_personal_ranker_version_id_p_3cdd; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.review_prompt_revisions
    ADD CONSTRAINT fk_review_prompt_revisions_personal_ranker_version_id_p_3cdd FOREIGN KEY (personal_ranker_version_id) REFERENCES public.personal_ranker_versions(id) ON DELETE SET NULL;


--
-- Name: review_reanalysis_runs fk_review_reanalysis_runs_program_id_library_programs; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.review_reanalysis_runs
    ADD CONSTRAINT fk_review_reanalysis_runs_program_id_library_programs FOREIGN KEY (program_id) REFERENCES public.library_programs(id) ON DELETE CASCADE;


--
-- Name: review_reanalysis_runs fk_review_reanalysis_runs_requested_by_users; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.review_reanalysis_runs
    ADD CONSTRAINT fk_review_reanalysis_runs_requested_by_users FOREIGN KEY (requested_by) REFERENCES public.users(id) ON DELETE SET NULL;


--
-- Name: analysis_saved_filters fk_saved_filter_owner; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.analysis_saved_filters
    ADD CONSTRAINT fk_saved_filter_owner FOREIGN KEY (owner_id) REFERENCES public.users(id);


--
-- Name: search_backfill_runs fk_search_backfill_runs_created_by_users; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.search_backfill_runs
    ADD CONSTRAINT fk_search_backfill_runs_created_by_users FOREIGN KEY (created_by) REFERENCES public.users(id) ON DELETE RESTRICT;


--
-- Name: sessions fk_sessions_user_id_users; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sessions
    ADD CONSTRAINT fk_sessions_user_id_users FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: similarity_group_members fk_similarity_group_members_asset_id_assets; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.similarity_group_members
    ADD CONSTRAINT fk_similarity_group_members_asset_id_assets FOREIGN KEY (asset_id) REFERENCES public.assets(id) ON DELETE CASCADE;


--
-- Name: similarity_group_members fk_similarity_group_members_group_id_similarity_groups; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.similarity_group_members
    ADD CONSTRAINT fk_similarity_group_members_group_id_similarity_groups FOREIGN KEY (group_id) REFERENCES public.similarity_groups(id) ON DELETE CASCADE;


--
-- Name: similarity_groups fk_similarity_groups_campaign_id_cleanup_campaigns; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.similarity_groups
    ADD CONSTRAINT fk_similarity_groups_campaign_id_cleanup_campaigns FOREIGN KEY (campaign_id) REFERENCES public.cleanup_campaigns(id) ON DELETE CASCADE;


--
-- Name: similarity_groups fk_similarity_groups_library_program_id_library_programs; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.similarity_groups
    ADD CONSTRAINT fk_similarity_groups_library_program_id_library_programs FOREIGN KEY (library_program_id) REFERENCES public.library_programs(id) ON DELETE SET NULL;


--
-- Name: tag_references fk_tag_references_connection_id_immich_connections; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tag_references
    ADD CONSTRAINT fk_tag_references_connection_id_immich_connections FOREIGN KEY (connection_id) REFERENCES public.immich_connections(id) ON DELETE CASCADE;


--
-- Name: task_failure_evidence fk_task_failure_evidence_analysis_generation_id_analysi_cc73; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_failure_evidence
    ADD CONSTRAINT fk_task_failure_evidence_analysis_generation_id_analysi_cc73 FOREIGN KEY (analysis_generation_id) REFERENCES public.analysis_generations(id) ON DELETE SET NULL;


--
-- Name: task_failure_evidence fk_task_failure_evidence_asset_id_assets; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_failure_evidence
    ADD CONSTRAINT fk_task_failure_evidence_asset_id_assets FOREIGN KEY (asset_id) REFERENCES public.assets(id) ON DELETE SET NULL;


--
-- Name: task_failure_evidence fk_task_failure_evidence_asset_revision_id_asset_revisions; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_failure_evidence
    ADD CONSTRAINT fk_task_failure_evidence_asset_revision_id_asset_revisions FOREIGN KEY (asset_revision_id) REFERENCES public.asset_revisions(id) ON DELETE SET NULL;


--
-- Name: task_failure_evidence fk_task_failure_evidence_model_version_id_model_versions; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_failure_evidence
    ADD CONSTRAINT fk_task_failure_evidence_model_version_id_model_versions FOREIGN KEY (model_version_id) REFERENCES public.model_versions(id) ON DELETE SET NULL;


--
-- Name: task_failure_evidence fk_task_failure_evidence_task_id_tasks; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_failure_evidence
    ADD CONSTRAINT fk_task_failure_evidence_task_id_tasks FOREIGN KEY (task_id) REFERENCES public.tasks(id) ON DELETE SET NULL;


--
-- Name: task_failure_evidence fk_task_failure_evidence_worker_id_workers; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_failure_evidence
    ADD CONSTRAINT fk_task_failure_evidence_worker_id_workers FOREIGN KEY (worker_id) REFERENCES public.workers(id) ON DELETE SET NULL;


--
-- Name: tasks fk_tasks_asset_id_assets; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tasks
    ADD CONSTRAINT fk_tasks_asset_id_assets FOREIGN KEY (asset_id) REFERENCES public.assets(id) ON DELETE SET NULL;


--
-- Name: tasks fk_tasks_asset_revision_id_asset_revisions; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tasks
    ADD CONSTRAINT fk_tasks_asset_revision_id_asset_revisions FOREIGN KEY (asset_revision_id) REFERENCES public.asset_revisions(id) ON DELETE SET NULL;


--
-- Name: tasks fk_tasks_campaign_id_cleanup_campaigns; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tasks
    ADD CONSTRAINT fk_tasks_campaign_id_cleanup_campaigns FOREIGN KEY (campaign_id) REFERENCES public.cleanup_campaigns(id) ON DELETE SET NULL;


--
-- Name: tasks fk_tasks_connection_id_immich_connections; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tasks
    ADD CONSTRAINT fk_tasks_connection_id_immich_connections FOREIGN KEY (connection_id) REFERENCES public.immich_connections(id) ON DELETE SET NULL;


--
-- Name: tasks fk_tasks_generation; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tasks
    ADD CONSTRAINT fk_tasks_generation FOREIGN KEY (analysis_generation_id) REFERENCES public.analysis_generations(id) ON DELETE SET NULL;


--
-- Name: tasks fk_tasks_leased_to_worker_id_workers; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tasks
    ADD CONSTRAINT fk_tasks_leased_to_worker_id_workers FOREIGN KEY (leased_to_worker_id) REFERENCES public.workers(id) ON DELETE SET NULL;


--
-- Name: tasks fk_tasks_parent_task_id_tasks; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tasks
    ADD CONSTRAINT fk_tasks_parent_task_id_tasks FOREIGN KEY (parent_task_id) REFERENCES public.tasks(id) ON DELETE CASCADE;


--
-- Name: tasks fk_tasks_processing_batch_id_processing_batches; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tasks
    ADD CONSTRAINT fk_tasks_processing_batch_id_processing_batches FOREIGN KEY (processing_batch_id) REFERENCES public.processing_batches(id) ON DELETE SET NULL;


--
-- Name: visual_embeddings fk_visual_embeddings_analysis_run_id_analysis_runs; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.visual_embeddings
    ADD CONSTRAINT fk_visual_embeddings_analysis_run_id_analysis_runs FOREIGN KEY (analysis_run_id) REFERENCES public.analysis_runs(id) ON DELETE CASCADE;


--
-- Name: visual_embeddings fk_visual_embeddings_asset_id_assets; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.visual_embeddings
    ADD CONSTRAINT fk_visual_embeddings_asset_id_assets FOREIGN KEY (asset_id) REFERENCES public.assets(id) ON DELETE CASCADE;


--
-- Name: visual_embeddings fk_visual_embeddings_asset_revision_id_asset_revisions; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.visual_embeddings
    ADD CONSTRAINT fk_visual_embeddings_asset_revision_id_asset_revisions FOREIGN KEY (asset_revision_id) REFERENCES public.asset_revisions(id) ON DELETE CASCADE;


--
-- Name: visual_embeddings fk_visual_embeddings_generation; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.visual_embeddings
    ADD CONSTRAINT fk_visual_embeddings_generation FOREIGN KEY (analysis_generation_id) REFERENCES public.analysis_generations(id) ON DELETE SET NULL;


--
-- Name: visual_fingerprint_bands fk_visual_fingerprint_bands_asset_id_assets; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.visual_fingerprint_bands
    ADD CONSTRAINT fk_visual_fingerprint_bands_asset_id_assets FOREIGN KEY (asset_id) REFERENCES public.assets(id) ON DELETE CASCADE;


--
-- Name: visual_fingerprint_bands fk_visual_fingerprint_bands_fingerprint_id_visual_fingerprints; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.visual_fingerprint_bands
    ADD CONSTRAINT fk_visual_fingerprint_bands_fingerprint_id_visual_fingerprints FOREIGN KEY (fingerprint_id) REFERENCES public.visual_fingerprints(id) ON DELETE CASCADE;


--
-- Name: visual_fingerprints fk_visual_fingerprints_analysis_run_id_analysis_runs; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.visual_fingerprints
    ADD CONSTRAINT fk_visual_fingerprints_analysis_run_id_analysis_runs FOREIGN KEY (analysis_run_id) REFERENCES public.analysis_runs(id) ON DELETE CASCADE;


--
-- Name: visual_fingerprints fk_visual_fingerprints_asset_id_assets; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.visual_fingerprints
    ADD CONSTRAINT fk_visual_fingerprints_asset_id_assets FOREIGN KEY (asset_id) REFERENCES public.assets(id) ON DELETE CASCADE;


--
-- Name: visual_fingerprints fk_visual_fingerprints_asset_revision_id_asset_revisions; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.visual_fingerprints
    ADD CONSTRAINT fk_visual_fingerprints_asset_revision_id_asset_revisions FOREIGN KEY (asset_revision_id) REFERENCES public.asset_revisions(id) ON DELETE CASCADE;


--
-- Name: worker_credentials fk_worker_credentials_replaced_by_id_worker_credentials; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.worker_credentials
    ADD CONSTRAINT fk_worker_credentials_replaced_by_id_worker_credentials FOREIGN KEY (replaced_by_id) REFERENCES public.worker_credentials(id) ON DELETE SET NULL;


--
-- Name: worker_credentials fk_worker_credentials_worker_id_workers; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.worker_credentials
    ADD CONSTRAINT fk_worker_credentials_worker_id_workers FOREIGN KEY (worker_id) REFERENCES public.workers(id) ON DELETE CASCADE;


--
-- Name: worker_enrollment_tokens fk_worker_enrollment_tokens_created_by_users; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.worker_enrollment_tokens
    ADD CONSTRAINT fk_worker_enrollment_tokens_created_by_users FOREIGN KEY (created_by) REFERENCES public.users(id);


--
-- Name: worker_model_availability fk_worker_model_availability_model_version_id_model_versions; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.worker_model_availability
    ADD CONSTRAINT fk_worker_model_availability_model_version_id_model_versions FOREIGN KEY (model_version_id) REFERENCES public.model_versions(id) ON DELETE CASCADE;


--
-- Name: worker_model_availability fk_worker_model_availability_worker_id_workers; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.worker_model_availability
    ADD CONSTRAINT fk_worker_model_availability_worker_id_workers FOREIGN KEY (worker_id) REFERENCES public.workers(id) ON DELETE CASCADE;


--
-- Name: workers fk_workers_affinity_processing_batch; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workers
    ADD CONSTRAINT fk_workers_affinity_processing_batch FOREIGN KEY (affinity_processing_batch_id) REFERENCES public.processing_batches(id) ON DELETE SET NULL;


--
-- Name: workspace_presets fk_workspace_presets_owner_id_users; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workspace_presets
    ADD CONSTRAINT fk_workspace_presets_owner_id_users FOREIGN KEY (owner_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: writeback_batches fk_writeback_batches_approval_session_id_sessions; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.writeback_batches
    ADD CONSTRAINT fk_writeback_batches_approval_session_id_sessions FOREIGN KEY (approval_session_id) REFERENCES public.sessions(id);


--
-- Name: writeback_batches fk_writeback_batches_created_by_users; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.writeback_batches
    ADD CONSTRAINT fk_writeback_batches_created_by_users FOREIGN KEY (created_by) REFERENCES public.users(id);


--
-- Name: writeback_operations fk_writeback_operations_asset_id_assets; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.writeback_operations
    ADD CONSTRAINT fk_writeback_operations_asset_id_assets FOREIGN KEY (asset_id) REFERENCES public.assets(id);


--
-- Name: writeback_operations fk_writeback_operations_asset_revision_id_asset_revisions; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.writeback_operations
    ADD CONSTRAINT fk_writeback_operations_asset_revision_id_asset_revisions FOREIGN KEY (asset_revision_id) REFERENCES public.asset_revisions(id) ON DELETE SET NULL;


--
-- Name: writeback_operations fk_writeback_operations_batch_id_writeback_batches; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.writeback_operations
    ADD CONSTRAINT fk_writeback_operations_batch_id_writeback_batches FOREIGN KEY (batch_id) REFERENCES public.writeback_batches(id) ON DELETE CASCADE;


--
-- Name: writeback_operations fk_writeback_operations_connection_id_immich_connections; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.writeback_operations
    ADD CONSTRAINT fk_writeback_operations_connection_id_immich_connections FOREIGN KEY (connection_id) REFERENCES public.immich_connections(id);


--
-- Name: writeback_operations fk_writeback_operations_inspected_by_users; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.writeback_operations
    ADD CONSTRAINT fk_writeback_operations_inspected_by_users FOREIGN KEY (inspected_by) REFERENCES public.users(id) ON DELETE SET NULL;


--
-- Name: highlight_comparisons highlight_comparisons_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.highlight_comparisons
    ADD CONSTRAINT highlight_comparisons_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.tasks(id) ON DELETE SET NULL;


--
-- Name: highlight_league_entries highlight_league_entries_asset_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.highlight_league_entries
    ADD CONSTRAINT highlight_league_entries_asset_id_fkey FOREIGN KEY (asset_id) REFERENCES public.assets(id) ON DELETE CASCADE;


--
-- Name: highlight_league_entries highlight_league_entries_asset_revision_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.highlight_league_entries
    ADD CONSTRAINT highlight_league_entries_asset_revision_id_fkey FOREIGN KEY (asset_revision_id) REFERENCES public.asset_revisions(id) ON DELETE CASCADE;


--
-- Name: highlight_league_entries highlight_league_entries_campaign_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.highlight_league_entries
    ADD CONSTRAINT highlight_league_entries_campaign_id_fkey FOREIGN KEY (campaign_id) REFERENCES public.cleanup_campaigns(id) ON DELETE SET NULL;


--
-- Name: highlight_league_entries highlight_league_entries_relative_rating_proposal_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.highlight_league_entries
    ADD CONSTRAINT highlight_league_entries_relative_rating_proposal_id_fkey FOREIGN KEY (relative_rating_proposal_id) REFERENCES public.proposals(id) ON DELETE SET NULL;


--
-- PostgreSQL database dump complete
--

