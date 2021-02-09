import sqlalchemy as sa
import pandas as pd
from dask import dataframe as dd
import posixpath
import functools
from sqlalchemy.sql import text
from . import _connection as conn
from . import model


class FeatureStore:
    def __init__(self, connection_string="sqlite:///bytehub.db"):
        """Create a Feature Store"""
        self.engine, self.session_maker = conn.connect(connection_string)
        model.Base.metadata.create_all(self.engine)

    @classmethod
    def _split_name(cls, namespace=None, name=None):
        """Parse namespace and name."""
        if not namespace and name and "/" in name:
            parts = name.split("/")
            namespace, name = parts[0], "/".join(parts[1:])
        return namespace, name

    @classmethod
    def _validate_kwargs(cls, args, valid=[], mandatory=[]):
        for name in args.keys():
            if name not in valid:
                raise ValueError(f"Invalid argument: {name}")
        for name in mandatory:
            if name not in args.keys():
                raise ValueError(f"Missing mandatory argument: {name}")

    @classmethod
    def _unpack_list(cls, obj, namespace=None):
        """Extract namespace, name combinations from DataFrame or list
        and return as list of tuples
        """
        if isinstance(obj, str):
            return [FeatureStore._split_name(namespace=namespace, name=obj)]
        elif isinstance(obj, pd.DataFrame):
            # DataFrame format must have a name column
            df = obj
            if "name" not in df.columns:
                raise ValueError("DataFrame must have a name column")
            return [
                (row.get("namespace", namespace), row.get("name"))
                for _, row in df.iterrows()
            ]
        elif isinstance(obj, list):
            # Could be list of names, of list of dictionaries
            r = []
            for item in obj:
                if isinstance(item, str):
                    r.append(FeatureStore._split_name(name=item, namespace=namespace))
                elif isinstance(item, dict):
                    r.append(
                        FeatureStore._split_name(
                            namespace=item.get("namespace"), name=item.get("name")
                        )
                    )
                else:
                    raise ValueError("List must contain strings or dicts")
            return r
        else:
            raise ValueError(
                "Must supply a string, dataframe or list specifying namespace/name"
            )

    def _list(self, cls, namespace=None, name=None, regex=None):
        namespace, name = FeatureStore._split_name(namespace, name)
        with conn.session_scope(self.session_maker) as session:
            r = session.query(cls)
            # Filter by namespace
            if namespace:
                r = r.filter_by(namespace=namespace)
            # Filter by matching name
            if name:
                r = r.filter_by(name=name)
            objects = r.all()
            df = pd.DataFrame([obj.as_dict() for obj in objects])
            if df.empty:
                return pd.DataFrame()
            # Filter by regex search on name
            if regex:
                df = df[df.name.str.contains(regex)]
            # Sort the columns
            column_order = ["namespace", "name", "version", "description", "meta"]
            column_order = [c for c in column_order if c in df.columns]
            df = df[[*column_order, *df.columns.difference(column_order)]]
            return df

    def _delete(self, cls, namespace=None, name=None):
        namespace, name = FeatureStore._split_name(namespace, name)
        with conn.session_scope(self.session_maker) as session:
            r = session.query(cls)
            if namespace:
                r = r.filter_by(namespace=namespace)
            if name:
                r = r.filter_by(name=name)
            obj = r.one_or_none()
            if not obj:
                raise RuntimeError(
                    f"No existing {cls.__name__} named {name} in {namespace}"
                )
            session.delete(obj)

    def _update(self, cls, namespace=None, name=None, payload={}):
        namespace, name = FeatureStore._split_name(namespace, name)
        with conn.session_scope(self.session_maker) as session:
            r = session.query(cls)
            if namespace:
                r = r.filter_by(namespace=namespace)
            if name:
                r = r.filter_by(name=name)
            obj = r.one_or_none()
            if not obj:
                raise RuntimeError(
                    f"No existing {cls.__name__} named {name} in {namespace}"
                )
            # Apply updates from payload
            obj.update_from_dict(payload)

    def _create(self, cls, namespace=None, name=None, payload={}):
        if cls is model.Namespace:
            payload.update({"name": name})
        else:
            namespace, name = FeatureStore._split_name(namespace, name)
            if not self._exists(model.Namespace, namespace=namespace):
                raise ValueError(f"{namespace} namespace does not exist")
            payload.update({"name": name, "namespace": namespace})
        with conn.session_scope(self.session_maker) as session:
            obj = cls()
            obj.update_from_dict(payload)
            session.add(obj)

    def _exists(self, cls, namespace=None, name=None):
        ls = self._list(cls, namespace=namespace, name=name)
        return not ls.empty

    def list_namespaces(self, **kwargs):
        """List namespaces in the feature store.

        Search by name or regex query.

        Args:
            name, str, optional: name of namespace to filter by.
            namespace, str, optional: same as name.
            regex, str, optional: regex filter on name.

        Returns:
            pd.DataFrame: DataFrame of namespaces and metadata.
        """

        self.__class__._validate_kwargs(kwargs, ["name", "namespace", "regex"])
        return self._list(
            model.Namespace,
            name=kwargs.get("name", kwargs.get("namespace")),
            regex=kwargs.get("regex"),
        )

    def create_namespace(self, name, **kwargs):
        """Create a new namespace in the feature store.

        Args:
            name, str: name of the namespace
            description, str, optional: description for this namespace
            url, str: url of data store
            storage_options, dict, optional: storage options to be passed to Dask
            meta, dict, optional: key/value pairs of metadata
        """
        self.__class__._validate_kwargs(
            kwargs,
            valid=["description", "url", "storage_options", "meta"],
            mandatory=["url"],
        )
        self._create(model.Namespace, name=name, payload=kwargs)

    def update_namespace(self, name, **kwargs):
        """Update a namespace in the feature store.

        Args:
            name, str: namespace to update
            description, str, optional: updated description
            storage_options, dict, optional: updated storage_options
            meta, dict, optional: updated key/value pairs of metadata
        """
        self.__class__._validate_kwargs(
            kwargs,
            valid=["description", "storage_options", "meta"],
        )
        self._update(model.Namespace, name=name, payload=kwargs)

    def delete_namespace(self, name):
        """Delete a namespace from the feature store.

        Args:
            name: namespace to be deleted.
        """
        if not self.list_features(namespace=name).empty:
            raise RuntimeError(
                f"{name} still contains features: these must be deleted first"
            )
        self._delete(model.Namespace, name=name)

    def list_features(self, **kwargs):
        """List features in the feature store.

        Search by namespace, name and/or regex query

        Args:
            name, str, optional: name of feature to filter by.
            namespace, str, optional: namespace to filter by.
            regex, str, optional: regex filter on name.

        Returns:
            pd.DataFrame: DataFrame of features and metadata.
        """

        FeatureStore._validate_kwargs(kwargs, valid=["name", "namespace", "regex"])
        return self._list(
            model.Feature,
            namespace=kwargs.get("namespace"),
            name=kwargs.get("name"),
            regex=kwargs.get("regex"),
        )

    def create_feature(self, name, namespace=None, **kwargs):
        """Create a new feature in the feature store.

        Args:
            name, str: name of the feature
            namespace, str, optional: namespace which should hold this feature
            description, str, optional: description for this namespace
            partition, str, optional: partitioning of stored timeseries (default: 'date')
            meta, dict, optional: key/value pairs of metadata
        """
        self.__class__._validate_kwargs(
            kwargs, valid=["description", "meta", "partition"], mandatory=[]
        )
        self._create(model.Feature, namespace=namespace, name=name, payload=kwargs)

    def delete_feature(self, name, namespace=None):
        """Delete a feature from the feature store.

        Args:
            name, str: name of feature to delete.
            namespace, str: namespace, if not included in feature name.
        """
        self._delete(model.Feature, namespace, name)

    def update_feature(self, name, namespace=None, **kwargs):
        """Update a namespace in the feature store.

        Args:
            name, str: feature to update
            namespace, str: namespace, if not included in feature name
            description, str, optional: updated description
            meta, dict, optional: updated key/value pairs of metadata
        """
        self.__class__._validate_kwargs(
            kwargs,
            valid=["description", "meta"],
        )
        self._update(model.Feature, name=name, namespace=namespace, payload=kwargs)

    def load_dataframe(
        self,
        features,
        from_date=None,
        to_date=None,
        freq=None,
        time_travel=None,
        mode="pandas",
    ):
        """Load a DataFrame of feature values from the feature store.

        Args:
            features, str, list, pd.DataFrame: name of feature to load, or list/DataFrame of feature namespaces/name
            from_date, datetime, optional: start date to load timeseries from, defaults to everything
            to_date, datetime, optional: end date to load timeseries to, defaults to everything
            freq, str, optional: frequency interval at which feature values should be sampled
            time_travel, optional:
            mode, str, optional: either 'pandas' (default) or 'dask' to specify the type of DataFrame to return

        Returns:
            pd.DataFrame or dask.DataFrame depending on mode
        """
        assert mode in ["dask", "pandas"], 'Mode must be either "dask" or "pandas"'
        dfs = []
        # Load each requested feature
        for f in self._unpack_list(features):
            namespace, name = f
            with conn.session_scope(self.session_maker) as session:
                feature = (
                    session.query(model.Feature)
                    .filter_by(name=name, namespace=namespace)
                    .one_or_none()
                )
                if not feature:
                    raise ValueError(f"No feature named {name} exists in {namespace}")
                # Load individual feature
                df = feature.load(
                    from_date=from_date,
                    to_date=to_date,
                    freq=freq,
                    time_travel=time_travel,
                    mode=mode,
                )
                dfs.append(df.rename(columns={"value": f"{namespace}/{name}"}))
        if mode == "pandas":
            return pd.concat(dfs, join="outer", axis=1).ffill()
        elif mode == "dask":
            dfs = functools.reduce(
                lambda left, right: dd.merge(
                    left, right, left_index=True, right_index=True, how="outer"
                ),
                dfs,
            )
            return dfs.ffill()
        else:
            raise NotImplementedError(f"{mode} has not been implemented")

    def save_dataframe(self, df, name=None, namespace=None):
        """Save a DataFrame of feature values to the feature store.

        Args:
            df, pd.DataFrame: DataFrame of feature values
                Must have a 'time' column or DateTimeIndex of time values
                Optionally include a 'created_time' (defaults to utcnow() if omitted)
                For a single feature a 'value' column, or column header of feature namespace/name
                For multiply features name the columns using namespace/name
            name, str, optional: Name of feature, if not included in DataFrame column name
            namespace, str, optional: Namespace, if not included in DataFrame column name
        """
        # Check dataframe columns
        feature_columns = df.columns.difference(["time", "created_time"])
        if len(feature_columns) == 1:
            # Single feature to save
            if feature_columns[0] == "value":
                if not name:
                    raise ValueError("Must specify feature name")
            else:
                name = feature_columns[0]
                df = df.rename(columns={name: "value"})
            if not self._exists(model.Feature, namespace, name):
                raise ValueError(f"Feature named {name} does not exist in {namespace}")
            # Save data for this feature
            namespace, name = self.__class__._split_name(namespace, name)
            with conn.session_scope(self.session_maker) as session:
                feature = (
                    session.query(model.Feature)
                    .filter_by(name=name, namespace=namespace)
                    .one()
                )
                # Save individual feature
                feature.save(df)
        else:
            # Multiple features in column names
            for feature_name in feature_columns:
                if not self._exists(model.Feature, namespace, name):
                    raise ValueError(
                        f"Feature named {name} does not exist in {namespace}"
                    )
            for feature_name in feature_columns:
                # Save individual features
                feature_df = df[[*df.columns.difference(feature_columns), feature_name]]
                self.save_dataframe(feature_df)

    def last(self, features):
        """Fetch the last value of one or more features

        Args:
            features, str, list, pd.DataFrame: feature or features to fetch

        Returns:
            dict: of name, value pairs
        """
        result = {}
        for f in self._unpack_list(features):
            namespace, name = f
            with conn.session_scope(self.session_maker) as session:
                feature = (
                    session.query(model.Feature)
                    .filter_by(name=name, namespace=namespace)
                    .one_or_none()
                )
                if not feature:
                    raise ValueError(f"No feature named {name} exists in {namespace}")
                # Load individual feature
                result[f"{namespace}/{name}"] = feature.last()
        return result

    def create_task(self):
        """Create a scheduled task to update the feature store."""
        raise NotImplementedError()

    def update_task(self):
        """Update a task."""
        raise NotImplementedError()

    def delete_task(self):
        """Delete a task."""
        raise NotImplementedError()