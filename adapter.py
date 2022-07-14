#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
BioCypher - CKG prototype
"""

import biocypher
import neo4j_utils as nu

from biocypher._logger import logger

logger.debug(f"Loading module {__name__}.")


class BioCypherAdapter:
    def __init__(
        self,
        dirname=None,
        db_name="neo4j",
        id_batch_size: int = 100000,
        user_schema_config_path="config/schema_config.yaml",
    ):

        self.db_name = db_name
        self.id_batch_size = id_batch_size

        # write driver
        self.bcy = biocypher.Driver(
            offline=True,  # set offline to true,
            # connect to running DB for input data via the neo4j driver
            user_schema_config_path=user_schema_config_path,
            delimiter="¦",
        )
        # start writer
        self.bcy.start_bl_adapter()
        self.bcy.start_batch_writer(dirname=dirname, db_name=self.db_name)

        # read driver
        self.driver = nu.Driver(
            db_name="neo4j",
            db_uri="bolt://localhost:7687",
            db_user="neo4j",
            db_passwd="your_password_here",
            multi_db=False,
        )

    def write_to_csv_for_admin_import(self):
        """
        Write nodes and edges to admin import csv files.
        """

        self.write_nodes()
        self.write_edges()
        self.bcy.write_import_call()

    def write_nodes(self):
        """
        Write nodes to admin import csv files.
        """

        # get node labels from csv
        with open("data/node_labels.csv", "r") as f:
            node_labels = f.read().splitlines()

        # remove Timepoint from node_labels
        node_labels.remove("Timepoint")

        # node_labels = []

        for label in node_labels:
            with self.driver.session() as session:
                session.read_transaction(
                    self._get_node_ids_and_write_batches_tx, label
                )

    def write_edges(self) -> None:
        """
        Write edges to admin import csv files.
        """

        # get node labels from csv
        with open("data/granular_relationships.txt", "r") as f:
            rel_labels = f.read().splitlines()

        rel_labels = rel_labels[1:]
        rel_labels = [label.split(",") for label in rel_labels]

        for src, typ, tar in rel_labels:

            # skip some types
            if not typ in [
                "VARIANT_FOUND_IN_CHROMOSOME",
                "LOCATED_IN",
                "HAS_STRUCTURE",
                "IS_SUBSTRATE_OF",
                "IS_QCMARKER_IN_TISSUE",
                "VARIANT_IS_CLINICALLY_RELEVANT",
                "IS_A_KNOWN_VARIANT",
            ]:

                with self.driver.session() as session:
                    session.read_transaction(
                        self._get_rel_ids_and_write_batches_tx,
                        src,
                        typ,
                        tar,
                    )

    def _get_node_ids_and_write_batches_tx(
        self,
        tx,
        label,
    ):
        """
        Write nodes to admin import csv files. Writer function needs to be
        performed inside the transaction.
        """

        result = tx.run(f"MATCH (n:{label}) " "RETURN id(n) as id")

        id_batch = []
        for record in result:
            # collect in batches
            if len(id_batch) < self.id_batch_size:

                id_batch.append(record["id"])

                # check if result depleted
                if result.peek() is None:

                    # write last batch
                    self._write_nodes(id_batch, label)

            # if full batch, trigger write process
            else:
                self._write_nodes(id_batch, label)
                id_batch = []

    def _get_rel_ids_and_write_batches_tx(
        self,
        tx,
        src,
        typ,
        tar,
    ):
        """
        Write edges to admin import csv files. Writer function needs to be
        performed inside the transaction.
        """

        result = tx.run(
            f"MATCH (n:{src})-[r:{typ}]->(m:{tar}) " "RETURN id(r) as id"
        )

        id_batch = []
        for record in result:
            # collect in batches
            if len(id_batch) < self.id_batch_size:
                id_batch.append(record["id"])

                # check if result depleted
                if result.peek() is None:
                    # write last batch
                    self._write_edges(id_batch, src, typ, tar)

            # if full batch, trigger write process
            else:
                self._write_edges(id_batch, src, typ, tar)
                id_batch = []

    def _write_nodes(self, id_batch, label):
        """
        Write edges to admin import csv files. Needs to be performed in a
        transaction.

        Args:

            id_batch: list of edge ids to write

            label: label of the node type
        """

        nodes = []
        with self.driver.session() as session:
            results = session.read_transaction(get_nodes_tx, id_batch)

            for res in results:

                _id = res["n"]["id"]
                _type = label
                _props = res["n"]
                nodes.append((_id, _type, _props))

        self.bcy.write_nodes(
            nodes=nodes,
            db_name=self.db_name,
        )

    def _write_edges(self, id_batch, src, typ, tar):
        """
        Write edges to admin import csv files. Needs to be performed in a
        transaction.

        Args:

            id_batch: list of edge ids to write

            src: source node label

            typ: relationship type

            tar: target node label
        """

        with self.driver.session() as session:
            rels = session.read_transaction(get_rels_tx, id_batch)

            edges = []
            for rel in rels:

                # extract relevant id
                _src = _process_node_id(rel["n"]["id"], src)
                _tar = _process_node_id(rel["m"]["id"], tar)

                # split some relationship types
                if typ in [
                    "MENTIONED_IN_PUBLICATION",
                    "ASSOCIATED_WITH",
                    "ANNOTATED_IN_PATHWAY",
                    "MAPS_TO",
                    "VARIANT_FOUND_IN_GENE",
                    "TRANSLATED_INTO",
                    "HAS_MODIFIED_SITE",
                ]:
                    _type = "_".join([typ, src, tar])
                else:
                    _type = typ
                _props = {}

                # add properties
                if typ in [
                    "ACTS_ON",
                    "COMPILED_INTERACTS_WITH",
                    "CURATED_INTERACTS_WITH",
                ]:
                    _props = {"type": typ}
                elif typ == "IS_BIOMARKER_OF_DISEASE":
                    props = rel["PROPERTIES(r)"]
                    _props = {
                        "age_range": props.get("age_range"),
                        "age_units": props.get("age_units"),
                        "assay": props.get("assay"),
                        "is_routine": props.get("is_routine"),
                        "is_used_in_clinic": props.get("is_used_in_clinic"),
                        "normal_range": props.get("normal_range"),
                        "sex": props.get("sex"),
                    }

                edges.append((_src, _tar, _type, _props))

            self.bcy.write_edges(
                edges=edges,
                db_name=self.db_name,
            )


def get_nodes_tx(tx, ids):
    result = tx.run(
        "MATCH (n) " "WHERE id(n) IN {ids} " "RETURN n",
        ids=ids,
    )
    return result.data()


def get_rels_tx(tx, ids):
    result = tx.run(
        "MATCH (n)-[r]->(m) "
        "WHERE id(r) IN {ids} "
        "RETURN n, PROPERTIES(r), m",
        ids=ids,
    )
    return result.data()


def _process_node_id(_id, _type):
    """
    Add prefixes to avoid multiple assignment.
    """
    if _type == "Food":
        _id = "FooDB:" + _id
    elif _type == "Chromosome":
        _id = "chr:" + _id
    elif _type == "Complex":
        _id = "CORUM:" + _id
    elif _type == "Timepoint":
        _id = "timepoint:" + _id
    elif _type == "Amino_acid_sequence":
        _id = "aas:" + _id
    elif _type == "Clinical_variable":
        _id = "snomedct:" + _id
    elif _type == "Publication":
        _id = "pmid:" + _id
    elif _type == "Somatic_mutation":
        _id = "somatic:" + _id
    elif _type == "Protein":
        _id = "uniprot:" + _id

    return _id