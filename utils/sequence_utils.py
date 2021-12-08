import logging
import os
import typing as t
import re
from collections import defaultdict
from enum import Enum
from functools import partial
from time import sleep

import Bio
import subprocess
from tqdm import tqdm
from urllib.error import HTTPError
import sys

sys.path.append("..")
from settings import get_settings

tqdm.pandas()

from Bio.Data import CodonTable
import numpy as np
import pandas as pd
from Bio import Entrez
from Bio.Seq import Seq

logger = logging.getLogger(__name__)

NUCLEOTIDES = ["A", "C", "G", "T"]
STOP_CODONS = CodonTable.standard_dna_table.stop_codons
CODONS = list(CodonTable.standard_dna_table.forward_table.keys()) + STOP_CODONS
AMINO_ACIDS = set(CodonTable.standard_dna_table.forward_table.values())


class SequenceType(Enum):
    GENOME = 1
    CDS = 2
    PROTEIN = 3


class DinucleotidePositionType(Enum):
    REGULAR = 1
    BRIDGE = 2
    NONBRIDGE = 3


class GenomeType(Enum):
    RNA = 0
    DNA = 1
    UNKNOWN = np.nan


class SequenceCollectingUtils:
    @staticmethod
    def parse_ncbi_sequence_raw_data_by_unique_acc(
        ncbi_raw_data: t.List[t.Dict[str, str]]
    ) -> t.List[t.Dict[str, str]]:
        """
        :param ncbi_raw_data: raw data from api efetch call to ncbi api
        :return: parsed ncbi data
        """

        acc_to_seq = {
            record["GBSeq_locus"]: record["GBSeq_sequence"]
            for record in ncbi_raw_data
            if "GBSeq_sequence" in record
        }
        acc_to_cds = {
            record["GBSeq_locus"]: ";".join(
                [
                    feature["GBFeature_location"]
                    for feature in record["GBSeq_feature-table"]
                    if feature["GBFeature_key"] == "CDS"
                ]
            )
            for record in ncbi_raw_data
        }
        for key in list(acc_to_cds.keys()):
            if acc_to_cds[key] == "":
                acc_to_cds.pop(key, None)
        acc_to_annotation = {
            record["GBSeq_locus"]: record["GBSeq_definition"]
            for record in ncbi_raw_data
            if "GBSeq_definition" in record
        }
        acc_to_keywords = {
            record["GBSeq_locus"]: record["GBSeq_keywords"]
            for record in ncbi_raw_data
            if "GBSeq_keywords" in record
        }
        parsed_data = [acc_to_seq, acc_to_cds, acc_to_annotation, acc_to_keywords]
        parsed_data = parsed_data

        return parsed_data

    @staticmethod
    def fill_ncbi_data_by_unique_acc(
        df: pd.DataFrame, parsed_data: t.List[t.Dict[str, str]]
    ):
        """
        :param df: dataframe to fill
        :param parsed_data: parsed data to fill df with
        :return: nothing. changes the df inplace
        """

        acc_to_seq = parsed_data[0]
        acc_to_cds = parsed_data[1]
        acc_to_annotation = parsed_data[2]
        acc_to_keywords = parsed_data[3]

        for col in ["sequence", "cds", "annotation", "keywords", "category"]:
            if col not in df.columns:
                df[col] = np.nan

        # replace values in acc field to exclude version number
        df["accession"] = df["accession"].apply(
            lambda x: str(x).split(".")[0].replace(" ", "").replace("*", "")
            if pd.notna(x)
            else x
        )

        df.set_index("accession", inplace=True)
        old_missing_seq_num = df["sequence"].isna().sum()
        logger.info(f"# extracted sequences = {len(acc_to_seq.keys())}")
        df["sequence"].fillna(value=acc_to_seq, inplace=True)
        new_missing_seq_num = df["sequence"].isna().sum()

        old_missing_cds_num = df["cds"].isna().sum()
        logger.info(f"# extracted cds = {len(acc_to_cds.keys())}")
        df["cds"].fillna(value=acc_to_cds, inplace=True)
        new_missing_cds_num = df["cds"].isna().sum()

        old_missing_annotations_num = df["annotation"].isna().sum()
        logger.info(f"# extracted annotations = {len(acc_to_annotation.keys())}")
        df["annotation"].fillna(value=acc_to_annotation, inplace=True)
        new_missing_annotations_num = df["annotation"].isna().sum()

        old_missing_kws_num = df["keywords"].isna().sum()
        logger.info(f"# extracted keywords = {len(acc_to_keywords.keys())}")
        df["keywords"].fillna(value=acc_to_keywords, inplace=True)
        new_missing_kws_num = df["keywords"].isna().sum()

        df["category"] = df["annotation"].apply(
            lambda x: "genome" if type(x) is str and "complete genome" in x else np.nan
        )
        df.reset_index(inplace=True)

        logger.info(
            f"dataframe filling is complete in pid {os.getpid()}, with {old_missing_seq_num - new_missing_seq_num} sequences filled, {old_missing_cds_num - new_missing_cds_num} cds regions filled, {old_missing_annotations_num - new_missing_annotations_num} annotations filled and {old_missing_kws_num - new_missing_kws_num} keywords filled"
        )

    @staticmethod
    def fill_missing_data_by_acc(df: pd.DataFrame) -> str:

        df_path = f"{os.getcwd()}/df_{SequenceCollectingUtils.fill_missing_data_by_acc.__name__}_pid_{os.getpid()}.csv"

        # first, handle non gi accessions
        accessions = [
            s.replace(" ", "").replace("*", "")
            for s in list(df.loc[df.source != "gi", "accession"].dropna().unique())
        ]
        if len(accessions) > 0:
            logger.info(
                f"performing efetch query to ncbi on {len(accessions)} genbank and refseq accessions"
            )
            ncbi_raw_data = SequenceCollectingUtils.do_ncbi_batch_fetch_query(
                accessions=accessions
            )
            parsed_data = (
                SequenceCollectingUtils.parse_ncbi_sequence_raw_data_by_unique_acc(
                    ncbi_raw_data=ncbi_raw_data
                )
            )
            SequenceCollectingUtils.fill_ncbi_data_by_unique_acc(
                df=df, parsed_data=parsed_data
            )

        # now, handle gi accessions
        accessions = [
            s.replace(" ", "").replace("*", "")
            for s in list(df.loc[df.source == "gi", "accession"].dropna().unique())
        ]
        if len(accessions) > 0:
            logger.info(
                f"performing efetch query to ncbi on {len(accessions)} gi accessions"
            )
            logger.info(
                f"performing efetch query to ncbi on {len(accessions)} gi accessions"
            )
            ncbi_raw_data = SequenceCollectingUtils.do_ncbi_batch_fetch_query(
                accessions=accessions
            )
            parsed_data = (
                SequenceCollectingUtils.parse_ncbi_sequence_raw_data_by_unique_acc(
                    ncbi_raw_data=ncbi_raw_data, is_gi_acc=True
                )
            )
            SequenceCollectingUtils.fill_ncbi_data_by_unique_acc(
                df=df, parsed_data=parsed_data, is_gi_acc=True
            )

        df["category"] = df["annotation"].apply(
            lambda x: "genome" if pd.notna(x) and "complete genome" in x else np.nan
        )

        df.to_csv(df_path, index=False)
        return df_path

    @staticmethod
    def flatten_sequence_data(
        df: pd.DataFrame,
        data_prefix: str = "virus",
    ) -> pd.DataFrame:
        """
        :param df: dataframe to flatten
        :param data_prefix: data prefix, for all column names
        :return: flattened dataframe
        """

        # remove data prefix
        flattened_df = df.rename(
            columns={
                col: col.replace(
                    f"{data_prefix}{'_' if len(data_prefix) > 0 else ''}", ""
                )
                for col in df.columns
            }
        )

        # set source by difference accession fields
        flattened_df["source"] = flattened_df[
            ["genbank_accession", "gi_accession"]
        ].apply(
            lambda x: "genbank"
            if pd.notna(x.genbank_accession)
            else ("gi" if pd.notna(x.gi_accession) else np.nan),
            axis=1,
        )

        # combine all the accession fields into a single accession field
        flattened_df.rename(columns={"genbank_accession": "accession"}, inplace=True)
        flattened_df["accession"].fillna(flattened_df["gi_accession"], inplace=True)
        flattened_df.drop("gi_accession", axis=1, inplace=True)
        flattened_df["accession"] = flattened_df["accession"].apply(
            lambda x: x.replace(",\s*", ";") if pd.notna(x) else x
        )

        # melt df by accession
        flattened_df = flattened_df.assign(
            accession=flattened_df.accession.str.split(";")
        ).explode("accession")
        flattened_df = flattened_df.set_index(
            flattened_df.groupby(level=0).cumcount(), append=True
        )
        flattened_df.index.rename(["index", "accession_genome_index"], inplace=True)
        flattened_df.reset_index(inplace=True)

        # add fields to fill
        flattened_df["sequence"] = np.nan
        flattened_df["cds"] = np.nan
        flattened_df["annotation"] = np.nan
        flattened_df["keywords"] = np.nan
        flattened_df["category"] = np.nan

        flattened_df.drop_duplicates(inplace=True)

        return flattened_df

    @staticmethod
    def do_ncbi_batch_fetch_query(accessions: t.List[str]) -> t.List[t.Dict[str, str]]:
        """
        :param accessions: list of accessions to batch query on
        :return: list of ncbi records corresponding to the accessions
        """
        ncbi_raw_records = []
        if len(accessions) == 0:
            return ncbi_raw_records
        retry = True
        while retry:
            try:
                ncbi_raw_records = list(
                    Entrez.parse(
                        Entrez.efetch(
                            db="nucleotide",
                            id=",".join([str(acc) for acc in accessions]),
                            retmode="xml",
                            api_key=get_settings().ENTREZ_API_KEY,
                        )
                    )
                )
                retry = False
            except HTTPError as e:
                if e.code == 429:
                    logger.info(f"Entrez query failed due to error {e}. will retry after a minute")
                    sleep(60)
                else:
                    logger.error(f"Failed Entrez query on {','.join([str(acc) for acc in accessions])} due to error {e}. will retry after a minute")
                    sleep(60)
        logger.info(
            f"collected {len(ncbi_raw_records)} records based on {len(accessions)} accessions"
        )
        return ncbi_raw_records

    @staticmethod
    def do_ncbi_search_queries(
        organisms: t.List[str], text_condition: str = "complete genome", do_via_genome_db: bool = False
    ) -> t.Dict[str, t.List[str]]:
        """
        :param organisms: list of organisms names to search
        :param text_condition: additional text condition to search by
        :param do_via_genome_db: indicator weather queries through the genome ncbi db should also be performed
        :return: map of organisms to their gi accessions
        """

        # perform direct search within the ncbi nucleotide databases (genbank and refseq)
        logger.info(
            f"performing {len(organisms)} esearch queries on [Organism] and text condition {text_condition}"
        )

        organism_to_accessions = defaultdict(list)

        logger.info(f"performing direct search within ncbi nucleotide databases for {len(organisms)} organism {text_condition} accessions")
        i = 0
        while i < len(organisms):
            if i % 50 == 0:
                logger.info(f"reached organism {i} out of {len(organisms)} within process {os.getpid()}")
            organism = organisms[i]
            try:
                raw_data = Entrez.read(
                    Entrez.esearch(
                        db="nucleotide",
                        term=f"({organisms[i]}[Organism]) AND {text_condition}[Text Word]",
                        retmode="xml",
                        idtype="acc",
                        api_key=get_settings().ENTREZ_API_KEY,
                    )
                )
                organism_to_accessions[organism] = organism_to_accessions[organism] + raw_data["IdList"]
                i += 1
                sleep(1)  # use 1 second interval to avoid more than 10 requests per second
            except HTTPError as e:
                if e.code == 429:
                    logger.info(
                        f"{os.getpid()} failed api request with error {e} and thus will sleep for a minute before trying again"
                    )
                    sleep(60)
                else:
                    logger.error(f"{os.getpid()} failed api request for tax {organisms[i]} with error {e}")
                    sleep(1)  # use 1 second interval to avoid more than 10 requests per second
                    i += 1

        # complement additional data based on each in genome db
        if do_via_genome_db:
            logger.info(
                f"performing indirect search within ncbi genome databases for {len(organisms)} organism {text_condition} accessions")
            i = 0
            while i < len(organisms):
                if i % 50 == 0:
                    logger.info(f"reached organism {i} out of {len(organisms)} within process {os.getpid()}")
                organism = organisms[i]
                cmd = f'esearch -db genome -query "{organism} complete genome" | epost -db genome | elink -target nuccore | efetch -format acc'
                ps = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                output = ps.communicate()[0]
                if ps.returncode == 0:
                    accession_regex = re.compile(r"[a-zA-Z]+\_*\d*\.*\d")
                    output_str = output.decode("utf-8")
                    accessions = [item for item in output_str.split("\n") if accession_regex.match(item)]
                    organism_to_accessions[organism] = organism_to_accessions[organism] + accessions
                    i += 1
                    sleep(1)  # sleep 1 second in between requests
                elif ps.returncode == 429 or "too many requests" in output.decode("utf-8"):
                    logger.error(f"exceeded number of requests to ncbi. will sleep for a minute")
                    sleep(60)
                else:
                    logger.error(f"failed to obtain accessions for {organism} due to error {ps.returncode}")
                    sleep(1) # sleep 1 second in between requests
                    i += 1

        return organism_to_accessions

    @staticmethod
    def fill_missing_data_by_organism(df: pd.DataFrame, tax_names_field: str = "taxon_name") -> str:
        """
        :param df: dataframe with sequence data to fill be taxa names based on thier search in the genome db
        :param tax_names_field: field name to extract query values from
        :return: path to filled dataframe
        """

        df_path = f"{os.getcwd()}/df_{SequenceCollectingUtils.fill_missing_data_by_organism.__name__}_pid_{os.getpid()}.csv"

        # find gi accessions for the given organism names
        organisms = list(df[tax_names_field].unique())
        if len(organisms) > 0:
            taxon_name_to_accessions = SequenceCollectingUtils.do_ncbi_search_queries(
                organisms=organisms
            )
            logger.info(
                f"accessions extracted for {len(taxon_name_to_accessions.keys())} out of {len(organisms)} taxa"
            )
            df.set_index(tax_names_field, inplace=True)
            df["accession"].fillna(value=taxon_name_to_accessions, inplace=True)
            df = df.explode(column="accession")
            df.reset_index(inplace=True)

            df.to_csv(df_path)

            # extract data based on the obtained gi accessions
            accessions = [
                str(item).replace(" ", "").replace("*", "")
                for item in list(df.accession.dropna().unique())
            ]
            if len(accessions) > 0:
                logger.info(
                    f"performing efetch query to ncbi on {len(accessions)} accessions"
                )
                ncbi_raw_data = SequenceCollectingUtils.do_ncbi_batch_fetch_query(
                    accessions=accessions
                )
                parsed_data = (
                    SequenceCollectingUtils.parse_ncbi_sequence_raw_data_by_unique_acc(
                        ncbi_raw_data=ncbi_raw_data
                    )
                )
                SequenceCollectingUtils.fill_ncbi_data_by_unique_acc(
                    df=df, parsed_data=parsed_data
                )

        df.to_csv(df_path, index=False)
        return df_path


class GenomeBiasCollectingService:
    @staticmethod
    def get_dinucleotides_by_range(coding_sequence: str, seq_range: range):
        """
        :param coding_sequence: coding sequence
        :param seq_range: range for sequence window
        :return: a sequence of bridge / non-bridge dinucleotides depending on requested range
        """
        dinuc_sequence = "".join([coding_sequence[i : i + 2] for i in seq_range])
        return dinuc_sequence

    @staticmethod
    def compute_dinucleotide_bias(
        sequence: str,
        computation_type: DinucleotidePositionType = DinucleotidePositionType.BRIDGE,
    ) -> t.Dict[str, float]:
        """
        :param sequence: a single coding sequences
        :param computation_type: can be either regular, or limited to bridge or non-bridge positions
        :return: dinucleotide bias dictionary
        dinculeotide bias computed according to https://science.sciencemag.org/content/sci/suppl/2018/10/31/362.6414.577.DC1/aap9072_Babayan_SM.pdf
        computation_type options:
            BRIDGE - consider only dinucleotide positions corresponding to bridges between codons (one is the last pos of a codon and the next is the first of another)
            NONBRIDGE - consider only dinucleotide positions do not correspond to bridges between codons
            REGULAR - consider all dinucleotide positions"""
        dinuc_sequence = sequence
        if (
            computation_type == DinucleotidePositionType.BRIDGE
        ):  # limit the sequence to bridge positions only
            dinuc_sequence = GenomeBiasCollectingService.get_dinucleotides_by_range(
                sequence, range(2, len(sequence) - 2, 3)
            )
        elif computation_type == DinucleotidePositionType.NONBRIDGE:
            dinuc_sequence = GenomeBiasCollectingService.get_dinucleotides_by_range(
                sequence, range(0, len(sequence) - 2, 3)
            )
        nucleotide_count = {
            "A": dinuc_sequence.count("A"),
            "C": dinuc_sequence.count("C"),
            "G": dinuc_sequence.count("G"),
            "T": dinuc_sequence.count("T"),
        }
        nucleotide_total_count = len(dinuc_sequence)
        dinucleotide_total_count = len(sequence) / 2
        dinucleotide_biases = dict()
        if nucleotide_total_count > 0 and dinucleotide_total_count > 0:
            for nuc_i in nucleotide_count.keys():
                for nuc_j in nucleotide_count.keys():
                    dinucleotide = nuc_i + nuc_j
                    try:
                        dinucleotide_biases[
                            f"{computation_type.name}_{nuc_i}p{nuc_j}_bias"
                        ] = (
                            sequence.count(dinucleotide) / dinucleotide_total_count
                        ) / (
                            nucleotide_count[nuc_i]
                            / nucleotide_total_count
                            * nucleotide_count[nuc_j]
                            / nucleotide_total_count
                        )
                    except Exception as e:
                        logger.error(
                            f"failed to compute dinucleotide bias for {dinucleotide} due to error {e} and will thus set it to nan"
                        )
                        dinucleotide_biases[
                            f"{computation_type.name}_{nuc_i}p{nuc_j}_bias"
                        ] = np.nan
        else:
            logger.error(
                f"dinucleotide sequence is of length {nucleotide_total_count} with {dinucleotide_total_count} dinucleotides in it, and thus dinucleotide bias cannot be computed"
            )

        return dinucleotide_biases

    @staticmethod
    def compute_codon_bias(coding_sequence: str) -> t.Dict[str, float]:
        """
        :param coding_sequence: a single coding sequences
        :return: the codon bias computation described in https://science.sciencemag.org/content/sci/suppl/2018/10/31/362.6414.577.DC1/aap9072_Babayan_SM.pdf
        """
        codon_biases = dict()
        for codon in CODONS:
            if codon not in STOP_CODONS:
                aa = Bio.Data.CodonTable.standard_dna_table.forward_table[codon]
                other_codons = [
                    codon
                    for codon in CODONS
                    if codon not in STOP_CODONS
                    and Bio.Data.CodonTable.standard_dna_table.forward_table[codon]
                    == aa
                ]
                codon_biases[codon + "_bias"] = coding_sequence.count(codon) / np.sum(
                    [coding_sequence.count(c) for c in other_codons]
                )
        return codon_biases

    @staticmethod
    def compute_diaa_bias(coding_sequence: str) -> t.Dict[str, float]:
        """
        :param coding_sequence: a single coding sequences
        :return: the diaa biases, similar to compute_dinucleotide_bias
        """
        sequence = str(Seq(coding_sequence).translate())
        diaa_biases = dict()
        aa_frequencies = {
            aa: sequence.count(aa) + 0.0001 for aa in AMINO_ACIDS
        }  # 0.0001 was added to avoid division by 0
        total_diaa_count = len(sequence) / 2
        total_aa_count = len(sequence)
        for aa_i in AMINO_ACIDS:
            for aa_j in AMINO_ACIDS:
                diaa = aa_i + aa_j
                diaa_biases[f"{aa_i}p{aa_j}_bias"] = (
                    sequence.count(diaa) / total_diaa_count
                ) / (
                    aa_frequencies[aa_i]
                    / total_aa_count
                    * aa_frequencies[aa_j]
                    / total_aa_count
                )
                if diaa_biases[f"{aa_i}p{aa_j}_bias"] == 0:
                    diaa_biases[f"{aa_i}p{aa_j}_bias"] += 0.0001
        return diaa_biases

    @staticmethod
    def compute_codon_pair_bias(
        coding_sequence: str, diaa_bias: t.Dict[str, float]
    ) -> t.Dict[str, float]:
        """
        :param coding_sequence: a single coding sequences
        :param diaa_bias: dictionary mapping diaa to its bias
        :return: dictionary mapping each dicodon to its bias
        codon pair bias measured by the codon pair score (CPS) as shown in https://science.sciencemag.org/content/sci/suppl/2018/10/31/362.6414.577.DC1/aap9072_Babayan_SM.pdf
        the denominator is obtained by multiplying the count od each codon with the bias of the respective amino acid pair
        """
        codon_count = dict()
        for codon in CODONS:
            codon_count[codon] = (
                coding_sequence.count(codon) + 0.0001
            )  # the 0.0001 addition prevents division by zero error
        codon_pair_scores = dict()
        for codon_i in CODONS:
            for codon_j in CODONS:
                if codon_i not in STOP_CODONS and codon_j not in STOP_CODONS:
                    codon_pair = codon_i + codon_j
                    codon_pair_count = coding_sequence.count(codon_pair)
                    denominator = (
                        codon_count[codon_i]
                        * codon_count[codon_j]
                        * diaa_bias[
                            f"{str(Seq(codon_i).translate())}{str(Seq(codon_j).translate())}_bias"
                        ]
                    )
                    if denominator == 0:
                        diaa_bias_value = diaa_bias[
                            f"{str(Seq(codon_i).translate())}{str(Seq(codon_j).translate())}_bias"
                        ]
                        logger.error(
                            f"denominator is 0 due to components being: codon_count[{codon_i}]={codon_count[codon_i]}, codon_count[{codon_j}]={codon_count[codon_j]}, diaa_bias={diaa_bias_value}"
                        )
                        pass
                    else:
                        codon_pair_scores[f"{codon_i}p{codon_j}_bias"] = float(
                            np.log(codon_pair_count / denominator)
                        )
        return codon_pair_scores

    @staticmethod
    def compute_mean_across_sequences(
        sequences: t.List[str], func: callable
    ) -> t.Dict[str, float]:
        """
        :param sequences: list of sequences to compute measures across
        :param func: function to use for computing measures
        :return: dictionary with the mean measures values across sequences
        """
        sequences_measures = [func(sequence) for sequence in sequences]
        measures_names = list(sequences_measures[0].keys())
        final_measures = {
            measure: np.sum([d[measure] for d in sequences_measures])
            / len(sequences_measures)
            for measure in measures_names
        }
        return final_measures

    @staticmethod
    def collect_genomic_bias_features(
        genome_sequence: str, coding_sequences: t.List[str]
    ):
        """
        :param genome_sequence: genomic sequence
        :param coding_sequences: coding sequence (if available)
        :return: dictionary with genomic features to be added as a record to a dataframe
        """
        genome_sequence = genome_sequence.upper()
        if len(coding_sequences) > 0:
            upper_coding_sequences = [
                coding_sequence.upper() for coding_sequence in coding_sequences
            ]
            coding_sequences = upper_coding_sequences
        logger.info(
            f"genomic sequence length={len(genome_sequence)} and {len(coding_sequences)} coding sequences"
        )
        dinucleotide_biases = GenomeBiasCollectingService.compute_dinucleotide_bias(
            sequence=genome_sequence,
            computation_type=DinucleotidePositionType.REGULAR,
        )
        id_genomic_traits = dict(dinucleotide_biases)

        if len(coding_sequences) > 0:
            id_genomic_traits.update(
                GenomeBiasCollectingService.compute_mean_across_sequences(
                    sequences=coding_sequences,
                    func=partial(
                        GenomeBiasCollectingService.compute_dinucleotide_bias,
                        computation_type=DinucleotidePositionType.BRIDGE,
                    ),
                )
            )

        id_genomic_traits.update(
            GenomeBiasCollectingService.compute_dinucleotide_bias(
                sequence=genome_sequence,
                computation_type=DinucleotidePositionType.NONBRIDGE,
            )
        )

        if len(coding_sequences) > 0:
            id_genomic_traits.update(
                GenomeBiasCollectingService.compute_mean_across_sequences(
                    sequences=coding_sequences,
                    func=GenomeBiasCollectingService.compute_diaa_bias,
                )
            )
            id_genomic_traits.update(
                GenomeBiasCollectingService.compute_mean_across_sequences(
                    sequences=coding_sequences,
                    func=partial(
                        GenomeBiasCollectingService.compute_codon_pair_bias,
                        diaa_bias=id_genomic_traits,
                    ),
                )
            )
        return id_genomic_traits

    @staticmethod
    def extract_coding_sequences(
        genomic_sequence: str, coding_regions: t.Union[float, str]
    ) -> t.List[str]:
        """
        :param genomic_sequence: genomic sequence
        :param coding_regions: list of coding sequence regions in the form of join(a..c,c..d,...), seperated by ";", or none if not available
        :return: the coding sequence
        """
        coding_region_regex = re.compile("(\d*)\.\.(\d*)")
        coding_sequences = []
        if pd.notna(coding_regions):
            for cds in coding_regions.split(";"):
                coding_sequence = ""
                for match in coding_region_regex.finditer(cds):
                    start = int(match.group(1))
                    try:
                        end = int(match.group(2))
                    except:
                        end = len(genomic_sequence)
                    coding_sequence += genomic_sequence[start - 1 : end]
                if (
                    len(coding_sequence) % 3 == 0 and len(coding_sequence) > 0
                ):  # ignore illegal coding sequences
                    coding_sequences.append(coding_sequence)
        return coding_sequences
