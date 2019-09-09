from __future__ import division
import rpy2.robjects as robjects
from rpy2.robjects.packages import importr
from rpy2.robjects import pandas2ri
import rpy2
from copy import copy
import pandas as pd
import numpy as np
from scipy.stats import t as t_dist
import matplotlib.pyplot as plt
from patsy import dmatrices
import seaborn as sns
import warnings
from joblib import Parallel, delayed
from pymer4.stats import _welch_ingredients
from pymer4.utils import (
    _sig_stars,
    _chunk_boot_ols_coefs,
    _chunk_perm_ols,
    _permute_sign,
    _ols,
    _ols_group,
    _corr_group,
    _perm_find,
    _return_t,
    to_ranks_by_group,
    rsquared,
    rsquared_adj,
)

__author__ = ["Eshin Jolly"]
__license__ = "MIT"

warnings.simplefilter("always", UserWarning)
pandas2ri.activate()


class Lmer(object):

    """
    Model class to hold data outputted from fitting lmer in R and converting to Python object. This class stores as much information as it can about a merMod object computed using lmer and lmerTest in R. Most attributes will not be computed until the fit method is called.

    Args:
        formula (str): Complete lmer-style model formula
        data (pandas.core.frame.DataFrame): input data
        family (string): what distribution family (i.e.) link function to use for the generalized model; default is gaussian (linear model)

    Attributes:
        fitted (bool): whether model has been fit
        formula (str): model formula
        data (pandas.core.frame.DataFrame): model copy of input data
        grps (dict): groups and number of observations per groups recognized by lmer
        AIC (float): model akaike information criterion
        logLike (float): model Log-likelihood
        family (string): model family
        ranef (pandas.core.frame.DataFrame/list): cluster-level differences from population parameters, i.e. difference between coefs and fixefs; returns list if multiple cluster variables are used to specify random effects (e.g. subjects and items)
        fixef (pandas.core.frame.DataFrame/list): cluster-level parameters; returns list if multiple cluster variables are used to specify random effects (e.g. subjects and items)
        coefs (pandas.core.frame.DataFrame/list): model summary table of population parameters
        resid (numpy.ndarray): model residuals
        fits (numpy.ndarray): model fits/predictions
        model_obj(lmer model): rpy2 lmer model object
        factors (dict): factors used to fit the model if any

    """

    def __init__(self, formula, data, family="gaussian"):

        self.family = family
        implemented_fams = [
            "gaussian",
            "binomial",
            "gamma",
            "inverse_gaussian",
            "poisson",
        ]
        if self.family not in implemented_fams:
            raise ValueError(
                "Family must be one of: gaussian, binomial, gamma, inverse_gaussian or poisson!"
            )
        self.fitted = False
        self.formula = formula.replace(" ", "")
        self.data = copy(data)
        self.grps = None
        self.AIC = None
        self.logLike = None
        self.warnings = []
        self.ranef_var = None
        self.ranef_corr = None
        self.ranef = None
        self.fixef = None
        self.design_matrix = None
        self.resid = None
        self.coefs = None
        self.model_obj = None
        self.factors = None
        self.ranked_data = False
        self.marginal_estimates = None
        self.marginal_contrasts = None
        self.sig_type = None
        self.factors_prev_ = None

    def __repr__(self):
        out = "{}.{}(fitted = {}, formula = {}, family = {})".format(
            self.__class__.__module__,
            self.__class__.__name__,
            self.fitted,
            self.formula,
            self.family,
        )
        return out

    def _make_factors(self, factor_dict, ordered=False):
        """
        Covert specific columns to R-style factors. Default scheme is dummy coding where reference is 1st level provided. Alternative is orthogonal polynomial contrasts. User can also specific custom contrasts.

        Args:
            factor_dict: (dict) dictionary with column names specified as keys, and lists of unique values to treat as factor levels
            ordered: (bool) whether to interpret factor_dict values as dummy-coded (1st list item is reference level) or as polynomial contrasts (linear contrast specified by ordered of list items)

        Returns:
            pandas.core.frame.DataFrame: copy of original data with factorized columns
        """
        if ordered:
            rstring = """
                function(df,f,lv){
                df[,f] <- factor(df[,f],lv,ordered=T)
                df
                }
            """
        else:
            rstring = """
                function(df,f,lv){
                df[,f] <- factor(df[,f],lv,ordered=F)
                df
                }
            """

        c_rstring = """
            function(df,f,c){
            contrasts(df[,f]) <- c(c)
            df
            }
            """

        factorize = robjects.r(rstring)
        contrastize = robjects.r(c_rstring)
        df = copy(self.data)
        for k in factor_dict.keys():
            df[k] = df[k].astype(str)

        #r_df = pandas2ri.py2ri(df)
        r_df = df
        for k, v in factor_dict.items():
            if isinstance(v, list):
                r_df = factorize(r_df, k, v)
            elif isinstance(v, dict):
                levels = list(v.keys())
                contrasts = np.array(list(v.values()))
                r_df = factorize(r_df, k, levels)
                r_df = contrastize(r_df, k, contrasts)
        return r_df

    def _refit_orthogonal(self):
        """
        Refit a model with factors organized as polynomial contrasts to ensure valid type-3 SS calculations with using `.anova()`. Previous factor specifications are stored in `model.factors_prev_`.
        """

        self.factors_prev_ = copy(self.factors)
        # Create orthogonal polynomial contrasts by just sorted factor levels alphabetically and letting R enumerate the required polynomial contrasts
        new_factors = {}
        for k in self.factors.keys():
            new_factors[k] = sorted(list(map(str, self.data[k].unique())))
        self.fit(
            factors=new_factors,
            ordered=True,
            summarize=False,
            permute=self._permute,
            conf_int=self._conf_int,
            REML=self._REML,
        )

    def anova(self, force_orthogonal=False):
        """
        Return a type-3 ANOVA table from a fitted model. Like R, this method does not ensure that contrasts are orthogonal to ensure correct type-3 SS computation. However, the force_orthogonal flag can refit the regression model with orthogonal polynomial contrasts automatically guaranteeing valid SS type 3 inferences. Note that this will overwrite factors specified in the last call to `.fit()`

        Args:
            force_orthogonal (bool): whether factors in the model should be recoded using polynomial contrasts to ensure valid type-3 SS calculations. If set to True, previous factor specifications will be saved in `model.factors_prev_`; default False

        Returns:
            anova_results (pd.DataFrame): Type 3 ANOVA results
        """

        if self.factors:
            # Model can only have factors if it's been fit
            if force_orthogonal:
                self._refit_orthogonal()
        elif not self.fitted:
            raise ValueError("Model must be fit before ANOVA table can be generated!")

        rstring = """
            function(model){
            df<- anova(model)
            df
            }
        """
        anova = robjects.r(rstring)
        self.anova_results = anova(self.model_obj)
        if self.anova_results.shape[1] == 6:
            self.anova_results.columns = [
                "SS",
                "MS",
                "NumDF",
                "DenomDF",
                "F-stat",
                "P-val",
            ]
            self.anova_results["Sig"] = self.anova_results["P-val"].apply(
                lambda x: _sig_stars(x)
            )
        elif self.anova_results.shape[1] == 4:
            warnings.warn(
                "MODELING FIT WARNING! Check model.warnings!! P-value computation did not occur because lmerTest choked. Possible issue(s): ranefx have too many parameters or too little variance..."
            )
            self.anova_results.columns = ["DF", "SS", "MS", "F-stat"]
        if force_orthogonal:
            print(
                "SS Type III Analysis of Variance Table with Satterthwaite approximated degrees of freedom:\n(NOTE: Model refit with orthogonal polynomial contrasts)"
            )
        else:
            print(
                "SS Type III Analysis of Variance Table with Satterthwaite approximated degrees of freedom:\n(NOTE: Using original model contrasts, orthogonality not guaranteed)"
            )
        return self.anova_results

    def _get_ngrps(self):
        """Get the groups information from the model as a dictionary
        """
        rstring="function(model){data.frame(unclass(summary(model))$ngrps)}"
        get_ngrps = robjects.r(rstring)
        res = get_ngrps(self.model_obj)
        if len(res.columns) != 1:
            raise Exception("Appears there's been another rpy2 or lme4 api change.")
        self.grps = res.to_dict()[res.columns[0]]

    def fit(
        self,
        conf_int="Wald",
        n_boot=500,
        factors=None,
        permute=None,
        ordered=False,
        summarize=True,
        verbose=False,
        REML=True,
        rank=False,
        rank_group="",
        rank_exclude_cols=[],
        no_warnings=False,
    ):
        """
        Main method for fitting model object. Will modify the model's data attribute to add columns for residuals and fits for convenience.

        Args:
            conf_int (str): which method to compute confidence intervals; 'profile', 'Wald' (default), or 'boot' (parametric bootstrap)
            n_boot (int): number of bootstrap intervals if bootstrapped confidence intervals are requests; default 500
            factors (dict): Keys should be column names in data to treat as factors. Values should either be a list containing unique variable levels if dummy-coding or polynomial coding is desired. Otherwise values should be another dictionary with unique variable levels as keys and desired contrast values (as specified in R!) as keys. See examples below
            permute (int): if non-zero, computes parameter significance tests by permuting test stastics rather than parametrically. Permutation is done by shuffling observations within clusters to respect random effects structure of data.
            ordered (bool): whether factors should be treated as ordered polynomial contrasts; this will parameterize a model with K-1 orthogonal polynomial regressors beginning with a linear contrast based on the factor order provided; default is False
            summarize (bool): whether to print a model summary after fitting; default is True
            verbose (bool): whether to print when and which model and confidence interval are being fitted
            REML (bool): whether to fit using restricted maximum likelihood estimation instead of maximum likelihood estimation; default True
            rank (bool): covert predictors in model formula to ranks by group prior to estimation. Model object will still contain original data not ranked data; default False
            rank_group (str): column name to group data on prior to rank conversion
            rank_exclude_cols (list/str): columns in model formula to not apply rank conversion to
            no_warnings (bool): turn off auto-printing warnings messages; warnings are always stored in the .warnings attribute; default False

        Returns:
            DataFrame: R style summary() table

        Examples:
            The following examples demonstrate how to treat variables as categorical factors.

            Dummy-Coding: Treat Col1 as a factor which 3 levels: A, B, C. Use dummy-coding with A as the reference level. Model intercept will be mean of A, and parameters will be B-A, and C-A.

            >>> model.fit(factors = {"Col1": ['A','B','C']})

            Orthogonal Polynomials: Treat Col1 as a factor which 3 levels: A, B, C. Estimate a linear contrast of C > B > A. Model intercept will be grand-mean of all levels, and parameters will be linear contrast, and orthogonal polynomial contrast (auto-computed).

            >>> model.fit(factors = {"Col1": ['A','B','C']}, ordered=True)

            Custom-contrast: Treat Col1 as a factor which 3 levels: A, B, C. Compare A to the mean of B and C. Model intercept will be the grand-mean of all levels, and parameters will be the desired contrast, a well as an automatically determined orthogonal contrast.

            >>> model.fit(factors = {"Col1": {'A': 1, 'B': -.5, 'C': -.5}}))

        """

        # Save params for future calls
        self._permute = permute
        self._conf_int = conf_int
        self._REML = REML
        if factors:
            dat = self._make_factors(factors, ordered)
            self.factors = factors
        else:
            dat = self.data
        if rank:
            if not rank_group:
                raise ValueError("rank_group must be provided if rank is True")
            dat = to_ranks_by_group(
                self.data, rank_group, self.formula, rank_exclude_cols
            )
            if factors and (set(factors.keys()) != set(rank_exclude_cols)):
                w = "Factors and ranks requested, but factors are not excluded from rank conversion. Are you sure you wanted to do this?"
                warnings.warn(w)
                self.warnings.append(w)
        if conf_int == "boot":
            self.sig_type = "bootstrapped"
        else:
            if permute:
                self.sig_type = "permutation" + " (" + str(permute) + ")"
            else:
                self.sig_type = "parametric"
        if self.family == "gaussian":
            _fam = "gaussian"
            if verbose:
                print(
                    "Fitting linear model using lmer with "
                    + conf_int
                    + " confidence intervals...\n"
                )

            lmer = importr("lmerTest")
            self.model_obj = lmer.lmer(self.formula, data=dat, REML=REML)
        else:
            if verbose:
                print(
                    "Fitting generalized linear model using glmer (family {}) with "
                    + conf_int
                    + " confidence intervals...\n".format(self.family)
                )
            lmer = importr("lme4")
            if self.family == "inverse_gaussian":
                _fam = "inverse.gaussian"
            elif self.family == "gamma":
                _fam = "Gamma"
            else:
                _fam = self.family
            self.model_obj = lmer.glmer(self.formula, data=dat, family=_fam, REML=REML)

        if permute and verbose:
            print("Using {} permutations to determine significance...".format(permute))
        base = importr("base")

        summary = base.summary(self.model_obj)
        unsum = base.unclass(summary)

        # Do scalars first cause they're easier

        # Get group names separately cause rpy2 > 2.9 is weird and doesnt return them above
        try:
            self._get_ngrps()
        except AttributeError:
            raise Exception("You appear to have an old version of rpy2, upgrade to >3.0")

        self.AIC = unsum.rx2("AICtab")[0]
        self.logLike = unsum.rx2("logLik")[0]

        # First check for lme4 printed messages (e.g. convergence info is usually here instead of in warnings)
        fit_messages = unsum.rx2("optinfo").rx2("conv").rx2("lme4").rx2("messages")
        # Then check warnings for additional stuff
        fit_warnings = unsum.rx2("optinfo").rx2("warnings")

        try:
            fit_warnings = [fw for fw in fit_warnings]
        except TypeError:
            fit_warnings = []
        try:
            fit_messages = [fm for fm in fit_messages]
        except TypeError:
            fit_messages = []

        fit_messages_warnings = fit_warnings + fit_messages
        if fit_messages_warnings:
            self.warnings.extend(fit_messages_warnings)
            if not no_warnings:
                for warning in self.warnings:
                    if isinstance(warning, list):
                        for w in warning:
                            print(w + " \n")
                    else:
                        print(warning + " \n")
        else:
            self.warnings = []
        # Coefficients, and inference statistics
        if self.family in ["gaussian", "gamma", "inverse_gaussian", "poisson"]:

            rstring = (
                """
                function(model){
                out.coef <- data.frame(unclass(summary(model))$coefficients)
                out.ci <- data.frame(confint(model,method='"""
                + conf_int
                + """',nsim="""
                + str(n_boot)
                + """))
                n <- c(rownames(out.ci))
                idx <- max(grep('sig',n))
                out.ci <- out.ci[-seq(1:idx),]
                out <- cbind(out.coef,out.ci)
                list(out,rownames(out))
                }
            """
            )
            estimates_func = robjects.r(rstring)
            out_summary, out_rownames = estimates_func(self.model_obj)
            df = out_summary
            dfshape = df.shape[1]
            df.index = out_rownames
            # df = pandas2ri.ri2py(estimates_func(self.model_obj))

            # gaussian
            if dfshape == 7:
                df.columns = [
                    "Estimate",
                    "SE",
                    "DF",
                    "T-stat",
                    "P-val",
                    "2.5_ci",
                    "97.5_ci",
                ]
                df = df[
                    ["Estimate", "2.5_ci", "97.5_ci", "SE", "DF", "T-stat", "P-val"]
                ]

            # gamma, inverse_gaussian
            elif dfshape == 6:
                if self.family in ["gamma", "inverse_gaussian"]:
                    df.columns = [
                        "Estimate",
                        "SE",
                        "T-stat",
                        "P-val",
                        "2.5_ci",
                        "97.5_ci",
                    ]
                    df = df[["Estimate", "2.5_ci", "97.5_ci", "SE", "T-stat", "P-val"]]
                else:
                    df.columns = [
                        "Estimate",
                        "SE",
                        "Z-stat",
                        "P-val",
                        "2.5_ci",
                        "97.5_ci",
                    ]
                    df = df[["Estimate", "2.5_ci", "97.5_ci", "SE", "Z-stat", "P-val"]]

            # Incase lmerTest chokes it won't return p-values
            elif dfshape == 5 and self.family == "gaussian":
                if not permute:
                    warnings.warn(
                        "MODELING FIT WARNING! Check model.warnings!! P-value computation did not occur because lmerTest choked. Possible issue(s): ranefx have too many parameters or too little variance..."
                    )
                    df.columns = ["Estimate", "SE", "T-stat", "2.5_ci", "97.5_ci"]
                    df = df[["Estimate", "2.5_ci", "97.5_ci", "SE", "T-stat"]]

        elif self.family == "binomial":

            rstring = (
                """
                function(model){
                out.coef <- data.frame(unclass(summary(model))$coefficients)
                out.ci <- data.frame(confint(model,method='"""
                + conf_int
                + """',nsim="""
                + str(n_boot)
                + """))
                n <- c(rownames(out.ci))
                idx <- max(grep('sig',n))
                out.ci <- out.ci[-seq(1:idx),]
                out <- cbind(out.coef,out.ci)
                odds <- exp(out.coef[1])
                colnames(odds) <- "OR"
                probs <- data.frame(sapply(out.coef[1],plogis))
                colnames(probs) <- "Prob"
                odds.ci <- exp(out.ci)
                colnames(odds.ci) <- c("OR_2.5_ci","OR_97.5_ci")
                probs.ci <- data.frame(sapply(out.ci,plogis))
                colnames(probs.ci) <- c("Prob_2.5_ci","Prob_97.5_ci")
                out <- cbind(out,odds,odds.ci,probs,probs.ci)
                list(out,rownames(out))
                }
            """
            )

            estimates_func = robjects.r(rstring)
            out_summary, out_rownames = estimates_func(self.model_obj)
            df = out_summary
            df.index = out_rownames
            # df = pandas2ri.ri2py(estimates_func(self.model_obj))

            df.columns = [
                "Estimate",
                "SE",
                "Z-stat",
                "P-val",
                "2.5_ci",
                "97.5_ci",
                "OR",
                "OR_2.5_ci",
                "OR_97.5_ci",
                "Prob",
                "Prob_2.5_ci",
                "Prob_97.5_ci",
            ]
            df = df[
                [
                    "Estimate",
                    "2.5_ci",
                    "97.5_ci",
                    "SE",
                    "OR",
                    "OR_2.5_ci",
                    "OR_97.5_ci",
                    "Prob",
                    "Prob_2.5_ci",
                    "Prob_97.5_ci",
                    "Z-stat",
                    "P-val",
                ]
            ]

        if permute:
            perm_dat = dat.copy()
            dv_var = self.formula.split("~")[0].strip()
            grp_vars = list(self.grps.keys())
            perms = []
            for i in range(permute):
                perm_dat[dv_var] = perm_dat.groupby(grp_vars)[dv_var].transform(
                    lambda x: x.sample(frac=1)
                )
                if self.family == "gaussian":
                    perm_obj = lmer.lmer(self.formula, data=perm_dat, REML=REML)
                else:
                    perm_obj = lmer.glmer(
                        self.formula, data=perm_dat, family=_fam, REML=REML
                    )
                perms.append(_return_t(perm_obj))
            perms = np.array(perms)
            pvals = []
            for c in range(df.shape[0]):
                if self.family in ["gaussian", "gamma", "inverse_gaussian"]:
                    pvals.append(_perm_find(perms[:, c], df["T-stat"][c]))
                else:
                    pvals.append(_perm_find(perms[:, c], df["Z-stat"][c]))
            df["P-val"] = pvals
            if "DF" in df.columns:
                df["DF"] = [permute] * df.shape[0]
                df = df.rename(columns={"DF": "Num_perm", "P-val": "Perm-P-val"})
            else:
                df["Num_perm"] = [permute] * df.shape[0]
                df = df.rename(columns={"P-val": "Perm-P-val"})

        if "P-val" in df.columns:
            df.loc[:,"Sig"] = df["P-val"].apply(lambda x: _sig_stars(x))
        elif "Perm-P-val" in df.columns:
            df.loc[:,"Sig"] = df["Perm-P-val"].apply(lambda x: _sig_stars(x))

        if (conf_int == "boot") and (permute is None):
            # We're computing parametrically bootstrapped ci's so it doesn't make sense to use approximation for p-values. Instead remove those from the output and make significant inferences based on whether the bootstrapped ci's cross 0.
            df = df.drop(columns=["P-val", "Sig"])
            if "DF" in df.columns:
                df = df.drop(columns="DF")
            df["Sig"] = df.apply(
                lambda row: "*" if row["2.5_ci"] * row["97.5_ci"] > 0 else "", axis=1
            )

        if permute:
            # Because all models except lmm have no DF column make sure Num_perm gets put in the right place
            cols = list(df.columns)
            col_order = cols[:-4] + ["Num_perm"] + cols[-4:-2] + [cols[-1]]
            df = df[col_order]
        self.coefs = df
        self.fitted = True

        # Random effect variances and correlations
        df = base.data_frame(unsum.rx2("varcor"))
        ran_vars = df.query("(var2 == 'NA') | (var2 == 'N')").drop("var2", axis=1)
        ran_vars.index = ran_vars["grp"]
        ran_vars.drop("grp", axis=1, inplace=True)
        ran_vars.columns = ["Name", "Var", "Std"]
        ran_vars.index.name = None
        ran_vars.replace("NA", "", inplace=True)

        ran_corrs = df.query("(var2 != 'NA') & (var2 != 'N')").drop("vcov", axis=1)
        if ran_corrs.shape[0] != 0:
            ran_corrs.index = ran_corrs["grp"]
            ran_corrs.drop("grp", axis=1, inplace=True)
            ran_corrs.columns = ["IV1", "IV2", "Corr"]
            ran_corrs.index.name = None
        else:
            ran_corrs = None

        self.ranef_var = ran_vars
        self.ranef_corr = ran_corrs

        # Cluster (e.g subject) level coefficients
        rstring = """
            function(model){
            out <- coef(model)
            out
            }
        """
        fixef_func = robjects.r(rstring)
        fixefs = fixef_func(self.model_obj)
        if len(fixefs) > 1:
            f_corrected_order = []
            for f in fixefs:
                f_corrected_order.append(
                    f[
                        list(self.coefs.index)
                        + [elem for elem in f.columns if elem not in self.coefs.index]
                    ]
                )
            self.fixef = f_corrected_order
            # self.fixef = [pandas2ri.ri2py(f) for f in fixefs]
        else:
            self.fixef = fixefs[0]
            self.fixef = self.fixef[
                list(self.coefs.index)
                + [elem for elem in self.fixef.columns if elem not in self.coefs.index]
            ]

        # Sort column order to match population coefs
        # This also handles cases in which random slope terms exist in the model without corresponding fixed effects terms, which generates extra columns in this dataframe. By default put those columns *after* the fixed effect columns of interest (i.e. population coefs)

        # Cluster (e.g subject) level random deviations
        rstring = """
            function(model){
            uniquify <- function(df){
            colnames(df) <- make.unique(colnames(df))
            df
            }
            out <- lapply(ranef(model),uniquify)
            out
            }
        """
        ranef_func = robjects.r(rstring)
        ranefs = ranef_func(self.model_obj)
        if len(ranefs) > 1:
            self.ranef = [r for r in ranefs]
        else:
            self.ranef = ranefs[0]            

        # Save the design matrix
        # Make sure column names match population coefficients
        stats = importr("stats")
        self.design_matrix = stats.model_matrix(self.model_obj)
        self.design_matrix = pd.DataFrame(
            self.design_matrix, columns=self.coefs.index[:]
        )

        # Model residuals
        rstring = """
            function(model){
            out <- resid(model)
            out
            }
        """
        resid_func = robjects.r(rstring)   
        try:
            self.data["residuals"] = copy(self.resid)
        except ValueError as e:
            print("**NOTE**: Column for 'residuals' not created in model.data, but saved in model.resid only. This is because you have rows with NaNs in your data.\n")

        # Model fits
        rstring = """
            function(model){
            out <- fitted(model)
            out
            }
        """
        fit_func = robjects.r(rstring)
        self.fits = fit_func(self.model_obj)
        try:
            self.data["fits"] = copy(self.fits)
        except ValueError as e:
            print("**NOTE** Column for 'fits' not created in model.data, but saved in model.fits only. This is because you have rows with NaNs in your data.\n")

        if summarize:
            return self.summary()

    def simulate(self, num_datasets, use_rfx=True):
        """
        Simulate new responses based upon estimates from a fitted model. By default group/cluster means for simulated data will match those of the original data. Unlike predict, this is a non-deterministic operation because lmer will sample random-efects values for all groups/cluster and then sample data points from their respective conditional distributions.

        Args:
            num_datasets (int): number of simulated datasets to generate. Each simulation always generates a dataset that matches the size of the original data
            use_rfx (bool): match group/cluster means in simulated data?; Default True

        Returns:
            ndarray: simulated data values
        """

        if isinstance(num_datasets, float):
            num_datasets = int(num_datasets)
        if not isinstance(num_datasets, int):
            raise ValueError("num_datasets must be an integer")

        if use_rfx:
            re_form = "NULL"
        else:
            re_form = "NA"

        rstring = (
            """
            function(model){
            out <- simulate(model,"""
            + str(num_datasets)
            + """,allow.new.levels=TRUE,re.form="""
            + re_form
            + """)
            out
            }
        """
        )
        simulate_func = robjects.r(rstring)
        sims = simulate_func(self.model_obj)
        return sims

    def predict(self, data, use_rfx=False, pred_type="response"):
        """
        Make predictions given new data. Input must be a dataframe that contains the same columns as the model.matrix excluding the intercept (i.e. all the predictor variables used to fit the model). If using random effects to make predictions, input data must also contain a column for the group identifier that were used to fit the model random effects terms. Using random effects to make predictions only makes sense if predictions are being made about the same groups/clusters.

        Args:
            data (pandas.core.frame.DataFrame): input data to make predictions on
            use_rfx (bool): whether to condition on random effects when making predictions
            pred_type (str): whether the prediction should be on the 'response' scale (default); or on the 'link' scale of the predictors passed through the link function (e.g. log-odds scale in a logit model instead of probability values)

        Returns:
            ndarray: prediction values

        """
        required_cols = self.design_matrix.columns[1:]
        if not all([col in data.columns for col in required_cols]):
            raise ValueError("Column names do not match all fixed effects model terms!")

        if use_rfx:
            required_cols = set(list(required_cols) + list(self.grps.keys()))
            if not all([col in data.columns for col in required_cols]):
                raise ValueError(
                    "Column names are missing random effects model grouping terms!"
                )

            re_form = "NULL"
        else:
            re_form = "NA"

        rstring = (
            """
            function(model,new){
            out <- predict(model,new,allow.new.levels=TRUE,re.form="""
            + re_form
            + """,type='"""
            + pred_type
            + """')
            out
            }
        """
        )

        predict_func = robjects.r(rstring)
        preds = predict_func(self.model_obj, data)
        return preds

    def summary(self):
        """
        Summarize the output of a fitted model.

        """

        if not self.fitted:
            raise RuntimeError("Model must be fitted to generate summary!")

        print("Formula: {}\n".format(self.formula))
        print("Family: {}\t Inference: {}\n".format(self.family, self.sig_type))
        print(
            "Number of observations: %s\t Groups: %s\n"
            % (self.data.shape[0], self.grps)
        )
        print("Log-likelihood: %.3f \t AIC: %.3f\n" % (self.logLike, self.AIC))
        print("Random effects:\n")
        print("%s\n" % (self.ranef_var.round(3)))
        if self.ranef_corr is not None:
            print("%s\n" % (self.ranef_corr.round(3)))
        else:
            print("No random effect correlations specified\n")
        print("Fixed effects:\n")
        return self.coefs.round(3)

    def post_hoc(
        self, marginal_vars, grouping_vars=None, p_adjust="tukey", summarize=True
    ):
        """
        Post-hoc pair-wise tests corrected for multiple comparisons (Tukey method) implemented using the emmeans package. This method provide both marginal means/trends along with marginal pairwise differences. More info can be found at: https://cran.r-project.org/web/packages/emmeans/emmeans.pdf

        Args:
            marginal_var (str/list): what variable(s) to compute marginal means/trends for; unique combinations of factor levels of these variable(s) will determine family-wise error correction
            grouping_vars (str/list): what variable(s) to group on. Trends/means/comparisons of other variable(s), will be computed at each level of these variable(s)
            p_adjust (str): multiple comparisons adjustment method. One of: tukey, bonf, fdr, hochberg, hommel, holm, dunnet, mvt (monte-carlo multi-variate T, aka exact tukey/dunnet). Default tukey
            summarize (bool): output effects and contrasts or don't (always stored in model object as model.marginal_estimates and model.marginal_contrasts); default True

        Returns:
            marginal_estimates (pd.Dataframe): unique factor level effects (e.g. means/coefs)
            marginal_contrasts (pd.DataFrame): contrasts between factor levels

        Examples:

            Pairwise comparison of means of A at each level of B

            >>> model.post_hoc(marginal_vars='A',grouping_vars='B')

            Pairwise differences of slopes of C between levels of A at each level of B

            >>> model.post_hoc(marginal_vars='C',grouping_vars=['A','B'])

            Pairwise differences of each unique A,B cell

            >>> model.post_hoc(marginal_vars=['A','B'])

        """

        if not marginal_vars:
            raise ValueError("Must provide marginal_vars")

        if not self.fitted:
            raise RuntimeError("Model must be fitted to generate post-hoc comparisons")

        if not isinstance(marginal_vars, list):
            marginal_vars = [marginal_vars]

        if grouping_vars and not isinstance(grouping_vars, list):
            grouping_vars = [grouping_vars]
            # Conditional vars can only be factor types
            if not all([elem in self.factors.keys() for elem in grouping_vars]):
                raise ValueError(
                    "All grouping_vars must be existing categorical variables (i.e. factors)"
                )

        # Need to figure out if marginal_vars is continuous or not to determine lstrends or emmeans call
        cont, factor = [], []
        for var in marginal_vars:
            if not self.factors or var not in self.factors.keys():
                cont.append(var)
            else:
                factor.append(var)

        if cont:
            if factor:
                raise ValueError(
                    "With more than one marginal variable, all variables must be categorical factors. Mixing continuous and categorical variables is not supported. Try passing additional categorical factors to grouping_vars"
                    ""
                )
            else:
                if len(cont) > 1:
                    raise ValueError(
                        "Marginal variables can only contain one continuous variable"
                    )
                elif len(cont) == 1:
                    if grouping_vars:
                        # Lstrends
                        cont = cont[0]
                        if len(grouping_vars) > 1:
                            g1 = grouping_vars[0]
                            _conditional = "+".join(grouping_vars[1:])

                            rstring = (
                                """
                                function(model){
                                suppressMessages(library(emmeans))
                                out <- lstrends(model,pairwise ~ """
                                + g1
                                + """|"""
                                + _conditional
                                + """,var='"""
                                + cont
                                + """',adjust='"""
                                + p_adjust
                                + """')
                                out
                                }"""
                            )
                        else:
                            rstring = (
                                """
                                function(model){
                                suppressMessages(library(emmeans))
                                out <- lstrends(model,pairwise ~ """
                                + grouping_vars[0]
                                + """,var='"""
                                + cont
                                + """',adjust='"""
                                + p_adjust
                                + """')
                                out
                                }"""
                            )

                    else:
                        raise ValueError(
                            "grouping_vars are required with a continuous marginal_vars"
                        )
        else:
            if factor:
                _marginal = "+".join(factor)
                if grouping_vars:
                    # emmeans with pipe
                    _conditional = "+".join(grouping_vars)
                    rstring = (
                        """
                        function(model){
                        suppressMessages(library(emmeans))
                        out <- emmeans(model,pairwise ~ """
                        + _marginal
                        + """|"""
                        + _conditional
                        + """, adjust='"""
                        + p_adjust
                        + """')
                        out
                        }"""
                    )
                else:
                    # emmeans without pipe
                    rstring = (
                        """
                        function(model){
                        suppressMessages(library(emmeans))
                        out <- emmeans(model,pairwise ~ """
                        + _marginal
                        + """,adjust='"""
                        + p_adjust
                        + """')
                        out
                        }"""
                    )
            else:
                raise ValueError("marginal_vars are not in model!")

        func = robjects.r(rstring)
        res = func(self.model_obj)
        base = importr("base")
        emmeans = importr("emmeans")

        # Marginal estimates
        # self.marginal_estimates = pandas2ri.ri2py(base.summary(res.rx2('emmeans')))
        self.marginal_estimates = base.summary(res)[0]
        # Resort columns
        effect_names = list(self.marginal_estimates.columns[:-4])
        # this column name changes depending on whether we're doing post-hoc trends or means
        effname = effect_names[-1]
        sorted = effect_names[:-1] + ["Estimate", "2.5_ci", "97.5_ci", "SE", "DF"]
        self.marginal_estimates = self.marginal_estimates.rename(
            columns={
                effname: "Estimate",
                "df": "DF",
                "lower.CL": "2.5_ci",
                "upper.CL": "97.5_ci",
            }
        )[sorted]

        # Marginal Contrasts
        self.marginal_contrasts = base.summary(res)[1].rename(
            columns={
                "t.ratio": "T-stat",
                "p.value": "P-val",
                "estimate": "Estimate",
                "df": "DF",
                "contrast": "Contrast",
            }
        )
        # Need to make another call to emmeans to get confidence intervals on contrasts
        confs = (
            base.unclass(emmeans.confint_emmGrid(res))[1]
            .iloc[:, -2:]
            .rename(columns={"lower.CL": "2.5_ci", "upper.CL": "97.5_ci"})
        )
        self.marginal_contrasts = pd.concat([self.marginal_contrasts, confs], axis=1)
        # Resort columns
        effect_names = list(self.marginal_contrasts.columns[:-7])
        sorted = effect_names + [
            "Estimate",
            "2.5_ci",
            "97.5_ci",
            "SE",
            "DF",
            "T-stat",
            "P-val",
        ]
        self.marginal_contrasts = self.marginal_contrasts[sorted]
        self.marginal_contrasts["Sig"] = self.marginal_contrasts["P-val"].apply(
            _sig_stars
        )

        if (
            p_adjust == "tukey"
            and self.marginal_contrasts.shape[0] >= self.marginal_estimates.shape[0]
        ):
            print(
                "P-values adjusted by tukey method for family of {} estimates".format(
                    self.marginal_contrasts["Contrast"].nunique()
                )
            )
        elif p_adjust != "tukey":
            print(
                "P-values adjusted by {} method for {} comparisons".format(
                    p_adjust, self.marginal_contrasts["Contrast"].nunique()
                )
            )
        if summarize:
            return self.marginal_estimates.round(3), self.marginal_contrasts.round(3)

    def plot_summary(
        self,
        figsize=(12, 6),
        error_bars="ci",
        ranef=True,
        axlim=None,
        intercept=True,
        ranef_alpha=0.5,
        coef_fmt="o",
        orient='v',
        **kwargs
    ):
        """
        Create a forestplot overlaying estimated coefficients with random effects (i.e. BLUPs). By default display the 95% confidence intervals computed during fitting.

        Args:
            error_bars (str): one of 'ci' or 'se' to change which error bars are plotted; default 'ci'
            ranef (bool): overlay BLUP estimates on figure; default True
            axlim (tuple): lower and upper limit of plot; default min and max of BLUPs
            intercept (bool): plot the intercept estimate; default True
            ranef_alpha (float): opacity of random effect points; default .5
            coef_fmt (str): matplotlib marker style for population coefficients

        Returns:
            matplotlib axis handle
        """

        if not self.fitted:
            raise RuntimeError("Model must be fit before plotting!")
        if orient not in ['h', 'v']:
            raise ValueError("orientation must be 'h' or 'v'")

        if isinstance(self.fixef, list):
            ranef_idx = kwargs.pop("ranef_idx", 0)
            print(
                "Multiple random effects clusters specified in model. Plotting the {} one. This can be changed by passing 'ranef_idx = number'".format(
                    ranef_idx + 1
                )
            )
            m_ranef = self.fixef[ranef_idx]
        else:
            m_ranef = self.fixef
        m_fixef = self.coefs

        if not intercept:
            m_ranef = m_ranef.drop("(Intercept)", axis=1)
            m_fixef = m_fixef.drop("(Intercept)", axis=0)

        if error_bars == "ci":
            col_lb = m_fixef["Estimate"] - m_fixef["2.5_ci"]
            col_ub = m_fixef["97.5_ci"] - m_fixef["Estimate"]
        elif error_bars == "se":
            col_lb, col_ub = m_fixef["SE"], m_fixef["SE"]

        # For seaborn
        m = pd.melt(m_ranef)

        f, ax = plt.subplots(1, 1, figsize=figsize)

        if ranef:
            alpha_plot = ranef_alpha
        else:
            alpha_plot = 0

        if orient == 'v':
            x_strip = 'value'
            x_err = m_fixef['Estimate']
            y_strip = 'variable'
            y_err = range(m_fixef.shape[0])
            xerr = [col_lb, col_ub]
            yerr = None
            ax.vlines(x=0, ymin=-1, ymax=self.coefs.shape[0], linestyles="--", color="grey")
            if not axlim:
                xlim = (m["value"].min() - 1, m["value"].max() + 1)
            else:
                xlim = axlim
            ylim = None
        else:
            y_strip = 'value'
            y_err = m_fixef['Estimate']
            x_strip = 'variable'
            x_err = range(m_fixef.shape[0])
            yerr = [col_lb, col_ub]
            xerr = None
            ax.hlines(y=0, xmin=-1, xmax=self.coefs.shape[0], linestyles="--", color="grey")
            if not axlim:
                ylim = (m["value"].min() - 1, m["value"].max() + 1)
            else:
                ylim = axlim
            xlim = None

        sns.stripplot(
            x=x_strip,
            y=y_strip,
            data=m,
            ax=ax,
            size=6,
            alpha=alpha_plot,
            color="grey",
        )

        ax.errorbar(
            x=x_err,
            y=y_err,
            xerr=xerr,
            yerr=yerr,
            fmt=coef_fmt,
            capsize=0,
            elinewidth=4,
            color="black",
            ms=12,
            zorder=9999999999,
        )
       
        ax.set(ylabel="", xlabel="Estimate", xlim=xlim, ylim=ylim)
        sns.despine(top=True, right=True, left=True)
        return ax

    def plot(
        self,
        param,
        figsize=(8, 6),
        xlabel="",
        ylabel="",
        plot_fixef=True,
        plot_ci=True,
        grps=[],
        ax=None,
    ):
        """
        Plot random and group level parameters from a fitted model

        Args:
            param (str): model parameter (column name) to plot
            figsize (tup): matplotlib desired figsize
            xlabel (str): x-axis label
            ylabel (str): y-axis label
            plot_fixef (bool): plot population effect fit of param?; default True
            plot_ci (bool): plot computed ci's of population effect?; default True
            grps (list): plot specific group fits only; must correspond to index values in model.fixef
            ax (matplotlib.axes.Axes): axis handle for an existing plot; if provided will ensure that random parameter plots appear *behind* all other plot objects.

        Returns:
            matplotlib axis handle

        """

        if not self.fitted:
            raise RuntimeError("Model must be fit before plotting!")
        if self.factors:
            raise NotImplementedError(
                "Plotting can currently only handle models with continuous predictors!"
            )
        if isinstance(self.fixef, list) or isinstance(self.ranef, list):
            raise NotImplementedError(
                "Plotting can currently only handle models with 1 random effect grouping variable!"
            )
        if not ax:
            f, ax = plt.subplots(1, 1, figsize=figsize)

        # Get range of unique values for desired parameter
        x_vals = self.design_matrix[param].unique()
        # Sort order to handle bug in matplotlib plotting
        idx = np.argsort(x_vals)

        # Get desired parameter part of the prediction
        fixef_pred = (
            self.coefs.loc["(Intercept)", "Estimate"]
            + self.coefs.loc[param, "Estimate"] * x_vals
        )
        fixef_pred_upper = (
            self.coefs.loc["(Intercept)", "97.5_ci"]
            + self.coefs.loc[param, "97.5_ci"] * x_vals
        )
        fixef_pred_lower = (
            self.coefs.loc["(Intercept)", "2.5_ci"]
            + self.coefs.loc[param, "2.5_ci"] * x_vals
        )

        if grps:
            if all(isinstance(x, int) for x in grps):
                ran_dat = self.fixef.iloc[grps, :]
            elif all(isinstance(x, str) for x in grps):
                ran_dat = self.fixef.loc[grps, :]
            else:
                raise TypeError(
                    "grps must be integer list for integer-indexing (.iloc) of fixed effects, or label list for label-indexing (.loc) of fixed effects"
                )
        else:
            ran_dat = self.fixef

        # Now generate random effects predictions
        for i, row in ran_dat.iterrows():

            ranef_desired = row["(Intercept)"] + row[param] * x_vals
            # ranef_other = np.dot(other_vals_means, row.loc[other_vals])
            pred = ranef_desired  # + ranef_other

            ax.plot(x_vals[idx], pred[idx], "-", linewidth=2)

        if plot_fixef:
            ax.plot(
                x_vals[idx],
                fixef_pred[idx],
                "--",
                color="black",
                linewidth=3,
                zorder=9999999,
            )

        if plot_ci:
            ax.fill_between(
                x_vals[idx],
                fixef_pred_lower[idx],
                fixef_pred_upper[idx],
                facecolor="black",
                alpha=0.25,
                zorder=9999998,
            )

        ax.set(
            ylim=(self.data.fits.min(), self.data.fits.max()),
            xlim=(x_vals.min(), x_vals.max()),
            xlabel=param,
            ylabel=self.formula.split("~")[0].strip(),
        )
        if xlabel:
            ax.set_xlabel(xlabel)
        if ylabel:
            ax.set_ylabel(ylabel)
        return ax


class Lm(object):

    """
    Model class to perform OLS regression. Formula specification works just like in R based on columns of a dataframe. Formulae are parsed by patsy which makes it easy to utilize specifiy columns as factors. This is **different** from Lmer. See patsy for more information on the different use cases.

    Args:
        formula (str): Complete lm-style model formula
        data (pandas.core.frame.DataFrame): input data
        family (string): what distribution family (i.e.) link function to use for the generalized model; default is gaussian (linear model)

    Attributes:
        fitted (bool): whether model has been fit
        formula (str): model formula
        data (pandas.core.frame.DataFrame): model copy of input data
        grps (dict): groups and number of observations per groups recognized by lmer
        AIC (float): model akaike information criterion
        logLike (float): model Log-likelihood
        family (string): model family
        ranef (pandas.core.frame.DataFrame/list): cluster-level differences from population parameters, i.e. difference between coefs and fixefs; returns list if multiple cluster variables are used to specify random effects (e.g. subjects and items)
        fixef (pandas.core.frame.DataFrame/list): cluster-level parameters; returns list if multiple cluster variables are used to specify random effects (e.g. subjects and items)
        coefs (pandas.core.frame.DataFrame/list): model summary table of population parameters
        resid (numpy.ndarray): model residuals
        fits (numpy.ndarray): model fits/predictions
        model_obj(lm model): rpy2 lmer model object
        factors (dict): factors used to fit the model if any

    """

    def __init__(self, formula, data, family="gaussian"):

        self.family = family
        # implemented_fams = ['gaussian','binomial']
        if self.family != "gaussian":
            raise NotImplementedError(
                "Currently only linear (family ='gaussian') models supported! "
            )
        self.fitted = False
        self.formula = formula.replace(" ", "")
        self.data = copy(data)
        self.AIC = None
        self.logLike = None
        self.warnings = []
        self.resid = None
        self.coefs = None
        self.model_obj = None
        self.factors = None
        self.ci_type = None
        self.se_type = None
        self.sig_type = None
        self.ranked_data = False
        self.estimator = None

    def __repr__(self):
        out = "{}.{}(fitted={}, formula={}, family={})".format(
            self.__class__.__module__,
            self.__class__.__name__,
            self.fitted,
            self.formula,
            self.family,
        )
        return out

    def fit(
        self,
        robust=False,
        conf_int="standard",
        permute=None,
        rank=False,
        summarize=True,
        verbose=False,
        n_boot=500,
        n_jobs=1,
        n_lags=1,
        cluster=None,
        weights=None,
        wls_dof_correction=True,
    ):
        """
        Fit a variety of OLS models. By default will fit a model that makes parametric assumptions (under a t-distribution) replicating the output of software like R. 95% confidence intervals (CIs) are also estimated parametrically by default. However, empirical bootstrapping can also be used to compute CIs; this procedure resamples with replacement from the data themselves, not residuals or data generated from fitted parameters and will be used for inference unless permutation tests are requested. Permutation testing will shuffle observations to generate a null distribution of t-statistics to perform inference on each regressor (permuted t-test).

        Alternatively, OLS robust to heteroscedasticity can be fit by computing sandwich standard error estimates (good ref: https://bit.ly/2VRb7jK). This is similar to Stata's robust routine. Of the choices below, 'hc1' or 'hc3' are amongst the more popular.  
        Robust estimators include:

        - 'hc0': Huber (1967) original sandwich estimator

        - 'hc1': Hinkley (1977) DOF adjustment to 'hc0' to account for small sample sizes (default)

        - 'hc2': different kind of small-sample adjustment of 'hc0' by leverage values in hat matrix

        - 'hc3': MacKinnon and White (1985) HC3 sandwich estimator; provides more robustness in smaller samples than hc2, Long & Ervin (2000)

        - 'hac': Newey-West (1987) estimator for robustness to heteroscedasticity as well as serial auto-correlation at given lags.

        - 'cluster' : cluster-robust standard errors (see Cameron & Miller 2015 for review). Provides robustness to errors that cluster according to specific groupings (e.g. repeated observations within a person/school/site). This acts as post-modeling "correction" for what a multi-level model explicitly estimates and is popular in the econometrics literature. DOF correction differs slightly from stat/statsmodels which use num_clusters - 1, where as pymer4 uses num_clusters - num_coefs

        Finally, weighted-least-squares (WLS) can be computed as an alternative to to hetereoscedasticity robust standard errors. This can be estimated by providing an array or series of weights (1 / variance of each group) with the same length as the number of observations or a column to use to compute group variances (which can be the same as the predictor column). This is often useful if some predictor(s) is categorical (e.g. dummy-coded) and taking into account unequal group variances is desired (i.e. in the simplest case this would be equivalent to peforming Welch's t-test). E.g:


        Args:
            robust (bool/str): whether to use heteroscedasticity robust s.e. and optionally which estimator type to use ('hc0','hc1', 'hc2', hc3','hac','cluster'). If robust = True, default robust estimator is 'hc1'; default False
            conf_int (str): whether confidence intervals should be computed through bootstrap ('boot') or assuming a t-distribution ('standard'); default 'standard'
            permute (int): if non-zero, computes parameter significance tests by permuting t-stastics rather than parametrically; works with robust estimators
            rank (bool): convert all predictors and dependent variable to ranks before estimating model; default False
            summarize (bool): whether to print a model summary after fitting; default True
            verbose (bool): whether to print which model, standard error, confidence interval, and inference type are being fitted
            n_boot (int): how many bootstrap resamples to use for confidence intervals (ignored unless conf_int='boot')
            n_jobs (int): number of cores for parallelizing bootstrapping or permutations; default 1
            n_lags (int): number of lags for robust estimator type 'hac' (ignored unless robust='hac'); default 1
            cluster (str): column name identifying clusters/groups for robust estimator type 'cluster' (ignored unless robust='cluster')
            weights (string/pd.Series/np.ndarray): weights to perform WLS instead of OLS. Pass in a column name in data to use to compute group variances and automatically adjust dof. Otherwise provide an array or series containing 1 / variance of each observation, in which case dof correction will not occur.
            wls_dof_correction (bool): whether to apply Welch-Satterthwaite approximate correction for dof when using weights based on an existing column in the data, ignored otherwise. Set to False to force standard dof calculation

        Returns:
            DataFrame: R style summary() table

        
        Examples:

            Simple regression that estimates a between groups t-test but not assuming equal variances  

            >>> model = Lm('DV ~ Group', data=df)

            Have pymer4 compute the between group variances automatically (preferred)  

            >>> model.fit(weights='Group') 

            Separately compute the variance of each group and use the inverse of that as the weights; dof correction won't be applied because its not trivial to compute  
            
            >>> weights = 1 / df.groupby("Group")['DV'].transform(np.var,ddof=1)
            model.fit(weights=weights)

        """
        if permute and permute < 500:
            w = "Permutation testing < 500 permutations is not recommended"
            warnings.warn(w)
            self.warnings.append(w)
        if robust:
            if isinstance(robust, bool):
                robust = "hc1"
            self.se_type = "robust" + " (" + robust + ")"
            if cluster:
                if cluster not in self.data.columns:
                    raise ValueError(
                        "cluster identifier must be an existing column in data"
                    )
                else:
                    cluster = self.data[cluster]
        else:
            self.se_type = "non-robust"

        if self.family == "gaussian":
            if verbose:
                if rank:
                    print_rank = "rank"
                else:
                    print_rank = "linear"
                if not robust:
                    print_robust = "non-robust"
                else:
                    print_robust = "robust " + robust

                if conf_int == "boot":
                    print(
                        "Fitting "
                        + print_rank
                        + " model with "
                        + print_robust
                        + " standard errors and \n"
                        + str(n_boot)
                        + "bootstrapped 95% confidence intervals...\n"
                    )
                else:
                    print(
                        "Fitting "
                        + print_rank
                        + " model with "
                        + print_robust
                        + " standard errors\nand 95% confidence intervals...\n"
                    )

                if permute:
                    print(
                        "Using {} permutations to determine significance...".format(
                            permute
                        )
                    )

        self.ci_type = (
            conf_int + " (" + str(n_boot) + ")" if conf_int == "boot" else conf_int
        )
        if (conf_int == "boot") and (permute is None):
            self.sig_type = "bootstrapped"
        else:
            if permute:
                self.sig_type = "permutation" + " (" + str(permute) + ")"
            else:
                self.sig_type = "parametric"

        # Parse formula using patsy to make design matrix
        if rank:
            self.ranked_data = True
            ddat = self.data.rank()
        else:
            self.ranked_data = False
            ddat = self.data

        # Handle weights if provided
        if isinstance(weights, str):
            if weights not in self.data.columns:
                raise ValueError(
                    "If weights is a string it must be a column that exists in the data"
                )
            else:
                dv = self.formula.split("~")[0]
                weight_groups = self.data.groupby(weights)
                weight_vals = 1 / weight_groups[dv].transform(np.var, ddof=1)
        else:
            weight_vals = weights
        if weights is None:
            self.estimator = "OLS"
        else:
            self.estimator = "WLS"

        y, x = dmatrices(self.formula, ddat, 1, return_type="dataframe")
        self.design_matrix = x

        # Compute standard estimates
        b, se, t, res = _ols(
            x,
            y,
            robust,
            all_stats=True,
            n_lags=n_lags,
            cluster=cluster,
            weights=weight_vals,
        )
        if cluster is not None:
            # Cluster corrected dof (num clusters - num coef)
            # Differs from stats and statsmodels which do num cluster - 1
            # Ref: http://cameron.econ.ucdavis.edu/research/Cameron_Miller_JHR_2015_February.pdf
            df = cluster.nunique() - x.shape[1]
        else:
            df = x.shape[0] - x.shape[1]
            if isinstance(weights, str) and wls_dof_correction:
                if weight_groups.ngroups != 2:
                    w = "Welch-Satterthwait DOF correction only supported for 2 groups in the data"
                    warnings.warn(w)
                    self.warnings.append(w)
                else:
                    welch_ingredients = np.array(
                        self.data.groupby(weights)[dv]
                        .apply(_welch_ingredients)
                        .values.tolist()
                    )
                    df = (
                        np.power(welch_ingredients[:, 0].sum(), 2)
                        / welch_ingredients[:, 1].sum()
                    )

        p = 2 * (1 - t_dist.cdf(np.abs(t), df))
        df = np.array([df] * len(t))
        sig = np.array([_sig_stars(elem) for elem in p])

        if conf_int == "boot":

            # Parallelize bootstrap computation for CIs
            par_for = Parallel(n_jobs=n_jobs, backend="multiprocessing")

            # To make sure that parallel processes don't use the same random-number generator pass in seed (sklearn trick)
            seeds = np.random.randint(np.iinfo(np.int32).max, size=n_boot)

            # Since we're bootstrapping coefficients themselves we don't need the robust info anymore
            boot_betas = par_for(
                delayed(_chunk_boot_ols_coefs)(
                    dat=self.data, formula=self.formula, weights=weights, seed=seeds[i]
                )
                for i in range(n_boot)
            )

            boot_betas = np.array(boot_betas)
            ci_u = np.percentile(boot_betas, 97.5, axis=0)
            ci_l = np.percentile(boot_betas, 2.5, axis=0)

        else:
            # Otherwise we're doing parametric CIs
            ci_u = b + t_dist.ppf(0.975, df) * se
            ci_l = b + t_dist.ppf(0.025, df) * se

        if permute:
            # Permuting will change degrees of freedom to num_iter and p-values
            # Parallelize computation
            # Unfortunate monkey patch that robust estimation hangs with multiple processes; maybe because of function nesting level??
            # _chunk_perm_ols -> _ols -> _robust_estimator
            if robust:
                n_jobs = 1
            par_for = Parallel(n_jobs=n_jobs, backend="multiprocessing")
            seeds = np.random.randint(np.iinfo(np.int32).max, size=permute)
            perm_ts = par_for(
                delayed(_chunk_perm_ols)(
                    x=x,
                    y=y,
                    robust=robust,
                    n_lags=n_lags,
                    cluster=cluster,
                    weights=weights,
                    seed=seeds[i],
                )
                for i in range(permute)
            )
            perm_ts = np.array(perm_ts)

            p = []
            for col, fit_t in zip(range(perm_ts.shape[1]), t):
                p.append(_perm_find(perm_ts[:, col], fit_t))
            p = np.array(p)
            df = np.array([permute] * len(p))
            sig = np.array([_sig_stars(elem) for elem in p])

        # Make output df
        results = np.column_stack([b, ci_l, ci_u, se, df, t, p, sig])
        results = pd.DataFrame(results)
        results.index = x.columns
        results.columns = [
            "Estimate",
            "2.5_ci",
            "97.5_ci",
            "SE",
            "DF",
            "T-stat",
            "P-val",
            "Sig",
        ]
        results[
            ["Estimate", "2.5_ci", "97.5_ci", "SE", "DF", "T-stat", "P-val"]
        ] = results[
            ["Estimate", "2.5_ci", "97.5_ci", "SE", "DF", "T-stat", "P-val"]
        ].apply(
            pd.to_numeric, args=("coerce",)
        )

        if permute:
            results = results.rename(columns={"DF": "Num_perm", "P-val": "Perm-P-val"})

        self.coefs = results
        self.fitted = True
        self.resid = res
        self.fits = y.squeeze() - res
        self.data["fits"] = y.squeeze() - res
        self.data["residuals"] = res

        # Fit statistics
        if 'Intercept' in self.design_matrix.columns:
            center_tss = True
        else:
            center_tss = False
        self.rsquared = rsquared(y.squeeze(), res, center_tss)
        self.rsquared_adj = rsquared_adj(self.rsquared, len(res), len(res) - x.shape[1], center_tss)
        half_obs = len(res) / 2.0
        ssr = np.dot(res, res.T)
        self.logLike = (-np.log(ssr) * half_obs) - (
            (1 + np.log(np.pi / half_obs)) * half_obs
        )
        self.AIC = 2 * x.shape[1] - 2 * self.logLike
        self.BIC = np.log((len(res))) * x.shape[1] - 2 * self.logLike

        if summarize:
            return self.summary()

    def summary(self):
        """
        Summarize the output of a fitted model.

        """

        if not self.fitted:
            raise RuntimeError("Model must be fit to generate summary!")

        print("Formula: {}\n".format(self.formula))
        print("Family: {}\t Estimator: {}\n".format(self.family, self.estimator))
        print(
            "Std-errors: {}\tCIs: {} 95%\tInference: {} \n".format(
                self.se_type, self.ci_type, self.sig_type
            )
        )
        print(
            "Number of observations: %s\t R^2: %.3f\t R^2_adj: %.3f\n"
            % (self.data.shape[0], self.rsquared, self.rsquared_adj)
        )
        print(
            "Log-likelihood: %.3f \t AIC: %.3f\t BIC: %.3f\n"
            % (self.logLike, self.AIC, self.BIC)
        )
        print("Fixed effects:\n")
        return self.coefs.round(3)

    def post_hoc(self):
        raise NotImplementedError(
            "Post-hoc tests are not yet implemented for linear models."
        )

    def to_corrs(self, corr_type="semi", ztrans_corrs=True):
        """
        For each predictor (except the intercept), compute the partial or semi-partial correlation of the of the predictor with the dependent variable for different interpretability. This does *not* change how inferences are performed, as they are always performed on the betas, not the correlation coefficients. Semi-partial corrs reflect the correlation between a predictor and the dv accounting for correlations between predictors; they are interpretable in the same way as the original betas. Partial corrs reflect the unique variance a predictor explains in the dv accounting for correlations between predictors *and* what is not explained by other predictors; this value is always >= the semi-partial correlation. Good ref: https://bit.ly/2GNwXh5
         Returns a pandas Series.

        Args:
            ztrans_partial_corrs (bool): whether to fisher z-transform (arctan) partial correlations before reporting them; default True

        """

        if not self.fitted:
            raise RuntimeError(
                "Model must be fit before partial correlations can be computed"
            )
        if corr_type not in ["semi", "partial"]:
            raise ValueError("corr_type must be 'semi' or 'partial'")
        from scipy.stats import pearsonr

        corrs = []
        corrs.append(np.nan)  # don't compute for intercept
        for c in self.design_matrix.columns[1:]:
            dv = self.formula.split("~")[0]
            other_preds = [e for e in self.design_matrix.columns[1:] if e != c]
            right_side = "+".join(other_preds)
            y, x = dmatrices(
                c + "~" + right_side, self.data, 1, return_type="dataframe"
            )
            pred_m_resid = _ols(
                x,
                y,
                robust=False,
                n_lags=1,
                cluster=None,
                all_stats=False,
                resid_only=True,
            )
            y, x = dmatrices(
                dv + "~" + right_side, self.data, 1, return_type="dataframe"
            )
            if corr_type == "semi":
                dv_m_resid = y.values.squeeze()
            elif corr_type == "partial":
                dv_m_resid = _ols(
                    x,
                    y,
                    robust=False,
                    n_lags=1,
                    cluster=None,
                    all_stats=False,
                    resid_only=True,
                )
            corrs.append(pearsonr(dv_m_resid, pred_m_resid)[0])
        if ztrans_corrs:
            corrs = np.arctanh(corrs)
        return pd.Series(corrs, index=self.coefs.index)

    def predict(self, data):
        """
        Make predictions given new data. Input must be a dataframe that contains the same columns as the model.matrix excluding the intercept (i.e. all the predictor variables used to fit the model). Will automatically use/ignore intercept to make a prediction if it was/was not part of the original fitted model.

        Args:
            data (pandas.core.frame.DataFrame): input data to make predictions on

        Returns:
            ndarray: prediction values

        """

        required_cols = self.design_matrix.columns[1:]
        if not all([col in data.columns for col in required_cols]):
            raise ValueError("Column names do not match all fixed effects model terms!")
        X = data[required_cols]
        coefs = self.coefs.loc[:, "Estimate"].values
        if self.coefs.index[0] == "Intercept":
            preds = np.dot(np.column_stack([np.ones(X.shape[0]), X]), coefs)
        else:
            preds = np.dot(X, coefs[1:])
        return preds


class Lm2(object):

    """
    Model class to perform two-stage OLS regression. Practically, a separate regression model is fit to each group in the data and then the coefficients from these regressions are entered into a second-level intercept only model (i.e. 1-sample t-test per coefficient). The results from this second level regression are reported. This is an alternative to using Lmer, as it implicitly allows intercept and slopes to vary by group, however with no prior/smoothing/regularization on the random effects. See https://bit.ly/2SwHhQU and Gelman (2005). This approach maybe less preferable to Lmer if the number of observations per group are few, but the number of groups is large, in which case the 1st-level estimates are much noisier and are not smoothed/regularized as in Lmer. It maybe preferable when a "maximal" rfx Lmer model is not estimable. Formula specification works just like in R based on columns of a dataframe. Formulae are parsed by patsy which makes it easy to utilize specific columns as factors. This is **different** from Lmer. See patsy for more information on the different use cases.

    Args:
        formula (str): Complete lm-style model formula
        data (pandas.core.frame.DataFrame): input data
        family (string): what distribution family (i.e.) link function to use for the generalized model; default is gaussian (linear model)
        group (list/string): the grouping variable to use to run the 1st-level regression; if a list is provided will run multiple levels feeding the coefficients from the previous level into the subsequent level

    Attributes:
        fitted (bool): whether model has been fit
        formula (str): model formula
        data (pandas.core.frame.DataFrame): model copy of input data
        grps (dict): groups and number of observations per groups recognized by lmer
        AIC (float): model akaike information criterion
        logLike (float): model Log-likelihood
        family (string): model family
        ranef (pandas.core.frame.DataFrame/list): cluster-level differences from population parameters, i.e. difference between coefs and fixefs; returns list if multiple cluster variables are used to specify random effects (e.g. subjects and items)
        fixef (pandas.core.frame.DataFrame/list): cluster-level parameters; returns list if multiple cluster variables are used to specify random effects (e.g. subjects and items)
        coefs (pandas.core.frame.DataFrame/list): model summary table of population parameters
        resid (numpy.ndarray): model residuals
        fits (numpy.ndarray): model fits/predictions
        model_obj(lm model): rpy2 lmer model object
        factors (dict): factors used to fit the model if any

    """

    def __init__(self, formula, data, group, family="gaussian"):

        self.family = family
        # implemented_fams = ['gaussian','binomial']
        if self.family != "gaussian":
            raise NotImplementedError(
                "Currently only linear (family ='gaussian') models supported! "
            )
        if isinstance(group, str):
            self.group = group
        else:
            raise TypeError("group must be a string or list")
        self.fitted = False
        self.formula = formula.replace(" ", "")
        self.data = copy(data)
        self.AIC = None
        self.logLike = None
        self.warnings = []
        self.resid = None
        self.fixef = None
        self.coefs = None
        self.model_obj = None
        self.factors = None
        self.ci_type = None
        self.se_type = None
        self.sig_type = None
        self.ranked_data = False
        self.iscorrs = False

    def __repr__(self):
        out = "{}.{}(fitted={}, formula={}, family={}, group={})".format(
            self.__class__.__module__,
            self.__class__.__name__,
            self.fitted,
            self.formula,
            self.family,
            self.group,
        )
        return out

    def fit(
        self,
        robust=False,
        conf_int="standard",
        permute=None,
        perm_on="t-stat",
        rank=False,
        summarize=True,
        verbose=False,
        n_boot=500,
        n_jobs=1,
        n_lags=1,
        to_corrs=False,
        ztrans_corrs=True,
        cluster=None,
    ):
        """
        Fit a variety of second-level OLS models; all 1st-level models are standard OLS. By default will fit a model that makes parametric assumptions (under a t-distribution) replicating the output of software like R. 95% confidence intervals (CIs) are also estimated parametrically by default. However, empirical bootstrapping can also be used to compute CIs, which will resample with replacement from the first level regression estimates and uses these CIs to perform inference unless permutation tests are requested. Permutation testing  will perform a one-sample sign-flipped permutation test on the estimates directly (perm_on='mean') or the t-statistic (perm_on='t-stat'). Permutation is a bit different than Lm which always permutes based on the t-stat.

        Alternatively, OLS robust to heteroscedasticity can be fit by computing sandwich standard error estimates (good ref: https://bit.ly/2VRb7jK). This is similar to Stata's robust routine.  
        Robust estimators include:

        - 'hc0': Huber (1980) original sandwich estimator
        
        - 'hc1': Hinkley (1977) DOF adjustment to 'hc0' to account for small sample sizes (default)

        - 'hc2': different kind of small-sample adjustment of 'hc0' by leverage values in hat matrix

        - 'hc3': MacKinnon and White (1985) HC3 sandwich estimator; provides more robustness in smaller samples than hc0, Long & Ervin (2000)

        - 'hac': Newey-West (1987) estimator for robustness to heteroscedasticity as well as serial auto-correlation at given lags.

        - 'cluster' : cluster-robust standard errors (see Cameron & Miller 2015 for review). Provides robustness to errors that cluster according to specific groupings (e.g. repeated observations within a person/school/site). This acts as post-modeling "correction" for what a multi-level model explicitly estimates and is popular in the econometrics literature. DOF correction differs slightly from stat/statsmodels which use num_clusters - 1, where as pymer4 uses num_clusters - num_coefs

        Args:
            robust (bool/str): whether to use heteroscedasticity robust s.e. and optionally which estimator type to use ('hc0','hc3','hac','cluster'). If robust = True, default robust estimator is 'hc0'; default False
            conf_int (str): whether confidence intervals should be computed through bootstrap ('boot') or assuming a t-distribution ('standard'); default 'standard'
            permute (int): if non-zero, computes parameter significance tests by permuting t-stastics rather than parametrically; works with robust estimators
            perm_on (str): permute based on a null distribution of the 'mean' of first-level estimates or the 't-stat' of first-level estimates; default 't-stat'
            rank (bool): convert all predictors and dependent variable to ranks before estimating model; default False
            to_corrs (bool/string): for each first level model estimate a semi-partial or partial correlations instead of betas and perform inference over these partial correlation coefficients. *note* this is different than Lm(); default False
            ztrans_corrs (bool): whether to fisher-z transform (arcsin) first-level correlations before running second-level model. Ignored if to_corrs is False; default True
            summarize (bool): whether to print a model summary after fitting; default True
            verbose (bool): whether to print which model, standard error, confidence interval, and inference type are being fitted
            n_boot (int): how many bootstrap resamples to use for confidence intervals (ignored unless conf_int='boot')
            n_jobs (int): number of cores for parallelizing bootstrapping or permutations; default 1
            n_lags (int): number of lags for robust estimator type 'hac' (ignored unless robust='hac'); default 1
            cluster (str): column name identifying clusters/groups for robust estimator type 'cluster' (ignored unless robust='cluster')

        Returns:
            DataFrame: R style summary() table

        """

        if robust:
            if isinstance(robust, bool):
                robust = "hc0"
            self.se_type = "robust" + " (" + robust + ")"
            if cluster:
                if cluster not in self.data.columns:
                    raise ValueError(
                        "cluster identifier must be an existing column in data"
                    )
                else:
                    cluster = self.data[cluster]
        else:
            self.se_type = "non-robust"
        self.ci_type = (
            conf_int + " (" + str(n_boot) + ")" if conf_int == "boot" else conf_int
        )
        if isinstance(to_corrs, str):
            if to_corrs not in ['semi', 'partial']:
                raise ValueError("to_corrs must be 'semi' or 'partial'")

        if (conf_int == "boot") and (permute is None):
            self.sig_type = "bootstrapped"
        else:
            if permute:
                if perm_on not in ["mean", "t-stat"]:
                    raise ValueError("perm_on must be 't-stat' or 'mean'")
                self.sig_type = "permutation" + " (" + str(permute) + ")"
            else:
                self.sig_type = "parametric"

        # Parallelize regression computation for 1st-level models
        par_for = Parallel(n_jobs=n_jobs, backend="multiprocessing")

        if rank:
            self.ranked_data = True
        else:
            self.ranked_data = False

        if to_corrs:
            # Loop over each group and get semi/partial correlation estimates
            # Reminder len(betas) == len(betas) - 1, from normal OLS, since corr of intercept is not computed
            betas = par_for(
                delayed(_corr_group)(
                    self.data,
                    self.formula,
                    self.group,
                    self.data[self.group].unique()[i],
                    self.ranked_data,
                    to_corrs,
                )
                for i in range(self.data[self.group].nunique())
            )
            if ztrans_corrs:
                betas = np.arctanh(betas)
            else:
                betas = np.array(betas)
        else:
            # Loop over each group and fit a separate regression
            betas = par_for(
                delayed(_ols_group)(
                    self.data,
                    self.formula,
                    self.group,
                    self.data[self.group].unique()[i],
                    self.ranked_data,
                )
                for i in range(self.data[self.group].nunique())
            )
            betas = np.array(betas)

        # Get the model matrix formula from patsy to make it more reliable to set the results dataframe index like Lmer
        y, x = dmatrices(self.formula, self.data, 1, return_type="dataframe")
        # Perform an intercept only regression for each beta
        results = []
        perm_ps = []
        for i in range(betas.shape[1]):
            df = pd.DataFrame({"X": np.ones_like(betas[:, i]), "Y": betas[:, i]})
            lm = Lm("Y ~ 1", data=df)
            lm.fit(
                robust=robust,
                conf_int=conf_int,
                summarize=False,
                n_boot=n_boot,
                n_jobs=n_jobs,
                n_lags=n_lags,
            )
            results.append(lm.coefs)
            if permute:
                # sign-flip permutation test for each beta instead to replace p-values
                seeds = np.random.randint(np.iinfo(np.int32).max, size=permute)
                par_for = Parallel(n_jobs=n_jobs, backend="multiprocessing")
                perm_est = par_for(
                    delayed(_permute_sign)(
                        data=betas[:, i], seed=seeds[j], return_stat=perm_on
                    )
                    for j in range(permute)
                )
                perm_est = np.array(perm_est)
                if perm_on == "mean":
                    perm_ps.append(_perm_find(perm_est, betas[:, i].mean()))
                else:
                    perm_ps.append(_perm_find(perm_est, lm.coefs["T-stat"].values))

        results = pd.concat(results, axis=0)
        ivs = self.formula.split("~")[-1].strip().split("+")
        ivs = [e.strip() for e in ivs]
        if to_corrs:
            intercept_pd = dict()
            for c in results.columns:
                intercept_pd[c] = np.nan
            intercept_pd = pd.DataFrame(intercept_pd, index=[0])
            results = pd.concat([intercept_pd, results], ignore_index=True)
        results.index = x.columns 
        self.coefs = results
        if to_corrs:
            self.fixef = pd.DataFrame(betas, columns=ivs)
        else:
            self.fixef = pd.DataFrame(betas, columns=x.columns)
        self.fixef.index = self.data[self.group].unique()
        self.fixef.index.name = self.group
        if permute:
            # get signifance stars
            sig = [_sig_stars(elem) for elem in perm_ps]
            # Replace dof and p-vales with permutation results
            if conf_int != "boot":
                self.coefs = self.coefs.drop(columns=["DF", "P-val"])
            if to_corrs:
                self.coefs["Num_perm"] = [np.nan] + [permute] * (
                    self.coefs.shape[0] - 1
                )
                self.coefs["Sig"] = [np.nan] + sig
                self.coefs["Perm-P-val"] = [np.nan] + perm_ps
            else:
                self.coefs["Num_perm"] = [permute] * self.coefs.shape[0]
                self.coefs["Sig"] = sig
                self.coefs["Perm-P-val"] = perm_ps
            self.coefs = self.coefs[
                [
                    "Estimate",
                    "2.5_ci",
                    "97.5_ci",
                    "SE",
                    "Num_perm",
                    "T-stat",
                    "Perm-P-val",
                    "Sig",
                ]
            ]
        self.fitted = True

        # Need to figure out how best to compute predictions and residuals. Should test how Lmer does it, i.e. BLUPs or fixed effects?
        # Option 1) Use only second-level estimates
        # Option 2) Use only first-level estimates and make separate predictions per group
        # self.resid = res
        # self.data['fits'] = y.squeeze() - res
        # self.data['residuals'] = res

        # Fit statistics
        self.rsquared = np.nan
        self.rsquared = np.nan
        self.rsquared_adj = np.nan
        self.logLike = np.nan
        self.AIC = np.nan
        self.BIC = np.nan
        self.iscorrs = to_corrs

        if summarize:
            return self.summary()

    def summary(self):
        """
        Summarize the output of a fitted model.

        """

        if not self.fitted:
            raise RuntimeError("Model must be fitted to generate summary!")

        print("Formula: {}\n".format(self.formula))
        print("Family: {}\n".format(self.family))
        print(
            "Std-errors: {}\tCIs: {} 95%\tInference: {} \n".format(
                self.se_type, self.ci_type, self.sig_type
            )
        )
        print(
            "Number of observations: %s\t Groups: %s\n"
            % (self.data.shape[0], {str(self.group): self.data[self.group].nunique()})
        )
        # print("R^2: %.3f\t R^2_adj: %.3f\n" %
        #       (self.data.shape[0], self.rsquared, self.rsquared_adj))
        # print("Log-likelihood: %.3f \t AIC: %.3f\t BIC: %.3f\n" %
        #       (self.logLike, self.AIC, self.BIC))
        print("Fixed effects:\n")
        if self.iscorrs:
            if self.iscorrs == "semi":
                corr = "semi-partial"
            else:
                corr = self.iscorrs
            print("Note: {} correlations reported".format(corr))
        return self.coefs.round(3)

    def plot_summary(
        self,
        figsize=(12, 6),
        error_bars="ci",
        ranef=True,
        axlim=None,
        ranef_alpha=0.5,
        coef_fmt="o",
        orient='v',
        **kwargs
    ):
        """
        Create a forestplot overlaying estimated coefficients with first-level effects. By default display the 95% confidence intervals computed during fitting.

        Args:
            error_bars (str): one of 'ci' or 'se' to change which error bars are plotted; default 'ci'
            ranef (bool): overlay BLUP estimates on figure; default True
            axlim (tuple): lower and upper limit of plot; default min and max of BLUPs
            ranef_alpha (float): opacity of random effect points; default .5
            coef_fmt (str): matplotlib marker style for population coefficients

        Returns:
            matplotlib axis handle
        """

        if not self.fitted:
            raise RuntimeError("Model must be fit before plotting!")
        if orient not in ['h', 'v']:
            raise ValueError("orientation must be 'h' or 'v'")

        m_ranef = self.fixef
        m_fixef = self.coefs.drop("Intercept", axis=0)

        if error_bars == "ci":
            col_lb = m_fixef["Estimate"] - m_fixef["2.5_ci"]
            col_ub = m_fixef["97.5_ci"] - m_fixef["Estimate"]
        elif error_bars == "se":
            col_lb, col_ub = m_fixef["SE"], m_fixef["SE"]

        # For seaborn
        m = pd.melt(m_ranef)

        f, ax = plt.subplots(1, 1, figsize=figsize)

        if ranef:
            alpha_plot = ranef_alpha
        else:
            alpha_plot = 0

        if orient == 'v':
            x_strip = 'value'
            x_err = m_fixef['Estimate']
            y_strip = 'variable'
            y_err = range(m_fixef.shape[0])
            xerr = [col_lb, col_ub]
            yerr = None
            ax.vlines(x=0, ymin=-1, ymax=self.coefs.shape[0], linestyles="--", color="grey")
            if not axlim:
                xlim = (m["value"].min() - 1, m["value"].max() + 1)
            else:
                xlim = axlim
            ylim = None
        else:
            y_strip = 'value'
            y_err = m_fixef['Estimate']
            x_strip = 'variable'
            x_err = range(m_fixef.shape[0])
            yerr = [col_lb, col_ub]
            xerr = None
            ax.hlines(y=0, xmin=-1, xmax=self.coefs.shape[0], linestyles="--", color="grey")
            if not axlim:
                ylim = (m["value"].min() - 1, m["value"].max() + 1)
            else:
                ylim = axlim
            xlim = None

        sns.stripplot(
            x=x_strip,
            y=y_strip,
            data=m,
            ax=ax,
            size=6,
            alpha=alpha_plot,
            color="grey"
        )

        ax.errorbar(
            x=x_err,
            y=y_err,
            xerr=xerr,
            yerr=yerr,
            fmt=coef_fmt,
            capsize=0,
            elinewidth=4,
            color="black",
            ms=12,
            zorder=9999999999,
        )
       
        ax.set(ylabel="", xlabel="Estimate", xlim=xlim, ylim=ylim)
        sns.despine(top=True, right=True, left=True)
        plt.tight_layout()
        return ax