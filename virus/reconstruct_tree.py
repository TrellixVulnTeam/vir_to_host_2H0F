import multiprocessing
import os
import pickle
import re
from functools import partial
import click
import pandas as pd
import numpy as np
import logging
from Bio import SeqIO


logger = logging.getLogger(__name__)

import sys
sys.path.append("..")
from utils.sequence_utils import SequenceCollectingUtils, SequenceType
from utils.parallelization_service import ParallelizationService
from utils.clustering_utils import ClusteringUtils

@click.command()
@click.option(
    "--input_path",
    type=click.Path(exists=True, file_okay=True, readable=True),
    help="path to dataframe holding the names of taxa for which a phylogeny should be reconstructed",
)
@click.option(
    "--leaf_element_field_name",
    type=click.Choice(["species_name", "taxon_name"], case_sensitive=False),
    help="name of field corresponding to elements in the input df that should correspond to leaves in the tree",
    required=False,
    default="species_name",
)
@click.option(
    "--workdir",
    type=click.Path(exists=False, file_okay=True, readable=True),
    help="directory to create pipeline input for: unaligned sequence data, aligned sequence data and reconstructed trees",
    required=False,
    default=f"{os.getcwd()}/reconstruct_tree/"
)
@click.option(
    "--sequence_annotation",
    type=click.Choice(["rna polymerase", "complete genome"], case_sensitive=False),
    help="text condition for sequence data search",
    required=False,
    default="rna polymerase",
)
@click.option(
    "--sequence_type",
    type=click.IntRange(1, 3),
    help="type of sequence data to collect:GENOME = 1, CDS = 2, PROTEIN = 3",
    required=False,
    default=3,
)
@click.option(
    "--log_path",
    type=click.Path(exists=False, file_okay=True, readable=True),
    help="path holding the logging of the script",
)
@click.option(
    "--output_path",
    type=click.Path(exists=False, file_okay=True, readable=True),
    help="path the reconstructed tree",
)
@click.option(
    "--debug_mode",
    type=click.BOOL,
    help="boolean indicating weather script should be executed in debug mode",
    required=False,
    default=False,
)
def reconstruct_tree(input_path: str,
                     leaf_element_field_name: str,
                     workdir: str,
                     sequence_annotation: str,
                     log_path: str,
                     output_path: str,
                     sequence_type: str,
                     debug_mode: bool):

    # initialize the logger
    logging.basicConfig(
        level=logging.DEBUG if debug_mode else logging.INFO,
        format="%(asctime)s module: %(module)s function: %(funcName)s line %(lineno)d: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_path),
        ],
    )

    # set up working environment in workdir
    logger.info(f"setting up working environment at {workdir}")
    unaligned_sequence_data_per_leaf_dir = f"{workdir}/unaligned_species_seq_data/"
    aligned_sequence_data_per_leaf_dir = f"{workdir}/aligned_species_seq_data/"
    similarities_values_per_leaf_dir = f"{workdir}/species_similarities_values/"
    os.makedirs(unaligned_sequence_data_per_leaf_dir, exist_ok=True)
    os.makedirs(aligned_sequence_data_per_leaf_dir, exist_ok=True)
    os.makedirs(similarities_values_per_leaf_dir, exist_ok=True)
    unaligned_sequence_data_path = f"{workdir}/unaligned_seq_data.fasta"
    representative_to_leaf_map_path = f"{workdir}/representative_to_leaf.pickle"
    aligned_sequence_data_path = f"{workdir}/aligned_seq_data.fasta"
    tree_log_path = f"{workdir}/tree_reconstruction.log"
    tree_path = output_path

    if not os.path.exists(tree_path) and not os.path.exists(aligned_sequence_data_path) and not os.path.exists(
            unaligned_sequence_data_path):

        # collect sequence data (multiple sequences per species)
        logger.info(f"collecting sequence data file per {leaf_element_field_name}")
        input_df = pd.read_csv(input_path)
        if leaf_element_field_name not in input_df.columns:
            logger.error(f"field {leaf_element_field_name} not in input df")
            raise ValueError(f"field {leaf_element_field_name} not in input df")

        input_df = ParallelizationService.parallelize(
            df=input_df,
            func=partial(SequenceCollectingUtils.fill_missing_data_by_organism, leaf_element_field_name, SequenceType(sequence_type), tuple(sequence_annotation)),
            num_of_processes=np.min([multiprocessing.cpu_count() - 1, 10]),
        )

        # select a representative sequence per species, and write representatives to a fasta file
        logger.info(f"selecting representative per {leaf_element_field_name}")
        representative_records = []
        representative_id_to_leaf = dict()
        input_df_by_leaf = input_df.groupby(leaf_element_field_name)
        for leaf in input_df_by_leaf.groups.keys():
            leaf_filename = re.sub('[^0-9a-zA-Z]+', '_', leaf)
            representative_record = ClusteringUtils.get_representative_by_msa(sequence_df=input_df_by_leaf.get_group(leaf),
                                                                              unaligned_seq_data_path=f"{unaligned_sequence_data_per_leaf_dir}{leaf_filename}.fasta",
                                                                              aligned_seq_data_path=f"{aligned_sequence_data_per_leaf_dir}{leaf_filename}.fasta",
                                                                              similarities_data_path=f"{similarities_values_per_leaf_dir}{leaf_filename}.csv")
            representative_id_to_leaf[representative_record.id] = leaf
            representative_records.append(representative_record)
        with open(representative_to_leaf_map_path, "wb") as outfile:
            pickle.dump(representative_id_to_leaf, file=outfile)
        SeqIO.write(representative_records, unaligned_sequence_data_path, format="fasta")

    # align written sequence data
    if not os.path.exists(tree_path) and not os.path.exists(aligned_sequence_data_path):
        logger.info(f"creating alignment from {unaligned_sequence_data_path} at {aligned_sequence_data_path}")
        res = ClusteringUtils.exec_mafft(input_path=unaligned_sequence_data_path, output_path=aligned_sequence_data_path)
        if res != 0:
            exit(1)

    # reconstruct ML tree
    if not os.path.exists(tree_path):
        logger.info(f"creating tree from {aligned_sequence_data_path} at {tree_path}")
        cmd = f"FastTree {'-nt ' if sequence_type != SequenceType.PROTEIN else ''}-log {tree_log_path} {aligned_sequence_data_path} > {tree_path}"
        res = os.system(cmd)
        if res != 0:
            error = ""
            if os.path.exists(tree_log_path):
                with open(tree_log_path, "r") as outfile:
                    error += outfile.read()
            logger.error(f"failed to reconstruct tree based on {aligned_sequence_data_path} due to error {error}")

if __name__ == '__main__':
    reconstruct_tree()